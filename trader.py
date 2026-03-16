import asyncio
import logging
import time

from py_clob_client.client import ClobClient
from py_clob_client.clob_types import (
    ApiCreds,
    MarketOrderArgs,
    OrderType,
    PartialCreateOrderOptions,
    BalanceAllowanceParams,
    AssetType,
)
from py_clob_client.order_builder.constants import BUY, SELL

import config
from state import BotState
from logger_setup import log_trade

logger = logging.getLogger("trader")


def _parse_status(result) -> tuple[str, str]:
    """Extract status and order ID from a post_order response."""
    if isinstance(result, dict):
        return (
            result.get("status", "UNKNOWN").upper(),
            result.get("orderID", "N/A"),
        )
    return (str(result).upper(), "N/A")


def _parse_fill_price(result, fallback: float) -> float:
    """Try to extract the average fill price from the order response."""
    if isinstance(result, dict):
        avg = result.get("averagePrice")
        if avg:
            return float(avg)
    return fallback


def _parse_matched_size(result) -> float | None:
    """Try to extract the actual matched size (shares) from the order response."""
    if isinstance(result, dict):
        for key in ("sizeMatched", "size_matched", "matchedSize", "matched_size"):
            val = result.get(key)
            if val is not None:
                try:
                    return float(val)
                except (ValueError, TypeError):
                    pass
    return None


class Trader:
    def __init__(self):
        self.client: ClobClient | None = None
        self.heartbeat_id: str = ""

    def initialize(self):
        """Derive API credentials and initialize the authenticated CLOB client."""
        logger.info("Deriving API credentials...")

        temp_client = ClobClient(
            host=config.CLOB_HOST,
            chain_id=config.CHAIN_ID,
            key=config.PRIVATE_KEY,
        )
        api_creds = temp_client.create_or_derive_api_creds()

        if api_creds is None:
            raise RuntimeError("Failed to derive API credentials")

        logger.info(f"API key derived: {api_creds.api_key[:12]}...")

        self.client = ClobClient(
            host=config.CLOB_HOST,
            chain_id=config.CHAIN_ID,
            key=config.PRIVATE_KEY,
            creds=api_creds,
            signature_type=config.SIGNATURE_TYPE,
            funder=config.FUNDER_ADDRESS,
        )

        ok = self.client.get_ok()
        logger.info(f"CLOB health check: {ok}")

    def get_usdc_balance(self) -> float:
        """Fetch current USDC.e balance available for trading."""
        try:
            result = self.client.get_balance_allowance(
                BalanceAllowanceParams(
                    asset_type=AssetType.COLLATERAL,
                    signature_type=config.SIGNATURE_TYPE,
                )
            )
            balance = float(result.get("balance", 0)) / 1e6  # USDC has 6 decimals
            return balance
        except Exception as e:
            logger.error(f"Failed to fetch USDC balance: {e}")
            return 0.0

    def get_token_balance(self, token_id: str) -> float:
        """Fetch actual conditional token balance (shares held)."""
        try:
            result = self.client.get_balance_allowance(
                BalanceAllowanceParams(
                    asset_type=AssetType.CONDITIONAL,
                    token_id=token_id,
                    signature_type=config.SIGNATURE_TYPE,
                )
            )
            balance = float(result.get("balance", 0)) / 1e6
            return balance
        except Exception as e:
            logger.debug(f"Failed to fetch token balance: {e}")
            return 0.0

    def buy(self, state: BotState, token_id: str, direction: str, worst_price: float = config.ENTRY_PRICE_MAX) -> bool:
        """
        Place a BUY market order (FOK) for the given token.
        On rejection, sets a cooldown so the strategy doesn't spam retries.
        Returns True if the order filled.
        """
        if config.ALL_IN:
            amount = self.get_usdc_balance()
            if amount < 0.50:
                logger.warning(f"Insufficient balance for all-in: ${amount:.2f}")
                return False
        else:
            amount = config.BET_SIZE

        logger.info(
            f"BUYING {direction} token: ${amount:.2f} @ worst ${worst_price:.2f} | "
            f"token={token_id[:16]}..."
        )

        state.buy_in_flight = True
        try:
            order = self.client.create_market_order(
                MarketOrderArgs(
                    token_id=token_id,
                    amount=amount,
                    side=BUY,
                    price=worst_price,
                ),
                PartialCreateOrderOptions(
                    tick_size=state.market_tick_size,
                    neg_risk=state.market_neg_risk,
                ),
            )
            result = self.client.post_order(order, OrderType.FOK)
            status, order_id = _parse_status(result)

            if status == "MATCHED":
                fill_price = _parse_fill_price(result, worst_price)

                # Determine actual shares received (fees are deducted in shares)
                shares = _parse_matched_size(result)
                if shares is None or shares <= 0:
                    shares = self.get_token_balance(token_id)
                if shares <= 0:
                    shares = amount / fill_price * 0.98  # conservative fallback
                    logger.warning(f"Could not determine exact shares, estimated: {shares:.4f}")

                logger.info(f"BUY filled: {shares:.4f} shares (response: {result})")
                state.open_position(token_id, direction, fill_price, shares)
                balance = self.get_usdc_balance()
                log_trade(
                    "BUY",
                    f"{direction} ${amount:.2f} @ ${fill_price:.4f} | shares={shares:.4f} | order={order_id}",
                    balance=balance,
                )
                return True

            # -- ORDER REJECTED --
            logger.warning(f"BUY rejected: status={status} | {result}")
            state.buy_blocked_until = time.time() + config.BUY_REJECT_COOLDOWN
            state.buy_in_flight = False
            return False

        except Exception as e:
            logger.error(f"BUY order error: {e}")
            state.buy_blocked_until = time.time() + config.BUY_REJECT_COOLDOWN
            state.buy_in_flight = False
            return False
        finally:
            state.buy_in_flight = False

    def sell(self, state: BotState, reason: str) -> bool:
        """
        Sell the current position. Escalation strategy:
          1. FOK  (all-or-nothing, up to SELL_MAX_RETRIES attempts)
          2. FAK  (partial fill, up to SELL_FAK_ATTEMPTS)
          3. Give up — let the market resolve naturally

        Returns True if the position was fully closed.
        """
        if not state.has_position():
            logger.warning("No position to sell")
            return False

        pos = state.position

        # Mark that we want to exit (strategy uses this to keep retrying)
        state.sell_pending = True
        state.sell_reason = reason

        total_attempts = state.sell_attempts
        use_fak = total_attempts >= config.SELL_MAX_RETRIES
        give_up = total_attempts >= (config.SELL_MAX_RETRIES + config.SELL_FAK_ATTEMPTS)

        if give_up:
            logger.error(
                f"SELL GAVE UP after {total_attempts} attempts — "
                f"letting market resolve. Reason={reason}"
            )
            # Don't retry anymore, just wait for resolution
            return False

        order_type = OrderType.FAK if use_fak else OrderType.FOK
        type_label = "FAK" if use_fak else "FOK"
        worst_price = 0.01  # accept any price to guarantee exit

        # Use actual token balance to avoid "not enough balance" errors
        actual_balance = self.get_token_balance(pos.token_id)
        sell_shares = actual_balance if actual_balance > 0 else pos.shares

        logger.info(
            f"SELLING ({reason}) {pos.side}: {sell_shares:.4f} shares "
            f"(stored={pos.shares:.4f} actual={actual_balance:.4f}) | "
            f"type={type_label} attempt={total_attempts + 1} @ worst ${worst_price:.2f}"
        )

        try:
            order = self.client.create_market_order(
                MarketOrderArgs(
                    token_id=pos.token_id,
                    amount=sell_shares,
                    side=SELL,
                    price=worst_price,
                ),
                PartialCreateOrderOptions(
                    tick_size=state.market_tick_size,
                    neg_risk=state.market_neg_risk,
                ),
            )
            result = self.client.post_order(order, order_type)
            status, order_id = _parse_status(result)

            if status == "MATCHED":
                exit_price = _parse_fill_price(
                    result, state.best_bid_for(pos.side) or pos.entry_price
                )
                pnl = (exit_price - pos.entry_price) * pos.shares
                state.close_position(exit_price, reason)
                balance = self.get_usdc_balance()
                log_trade(
                    reason,
                    f"{pos.side} exit @ ${exit_price:.4f} | "
                    f"entry=${pos.entry_price:.4f} | "
                    f"shares={pos.shares:.2f} | "
                    f"type={type_label} attempt={total_attempts + 1}",
                    pnl=pnl,
                    balance=balance,
                )
                return True

            # -- ORDER REJECTED --
            state.mark_sell_failed()

            if use_fak:
                # FAK may have partially filled — check if we still hold tokens
                # For now, assume nothing filled on rejection; a future improvement
                # would query the order to check size_matched.
                logger.warning(
                    f"SELL {type_label} rejected: status={status} | "
                    f"Will retry next tick ({total_attempts + 1}/{config.SELL_MAX_RETRIES + config.SELL_FAK_ATTEMPTS})"
                )
            else:
                logger.warning(
                    f"SELL FOK rejected: status={status} | "
                    f"attempt {total_attempts + 1}/{config.SELL_MAX_RETRIES}, "
                    f"will escalate to FAK after {config.SELL_MAX_RETRIES}"
                )
            return False

        except Exception as e:
            logger.error(f"SELL order error: {e}")
            state.mark_sell_failed()
            return False

    def cancel_all_orders(self):
        """Cancel all open orders."""
        try:
            result = self.client.cancel_all()
            logger.info(f"Cancelled all orders: {result}")
        except Exception as e:
            logger.debug(f"Cancel all failed (may have no orders): {e}")

    async def heartbeat_loop(self, state: BotState):
        """Send heartbeats to keep the CLOB session alive."""
        while state.running:
            try:
                result = self.client.post_heartbeat(self.heartbeat_id)
                if isinstance(result, dict):
                    self.heartbeat_id = result.get("heartbeat_id", self.heartbeat_id)
            except Exception as e:
                logger.debug(f"Heartbeat failed: {e}")
            await asyncio.sleep(config.HEARTBEAT_INTERVAL)
