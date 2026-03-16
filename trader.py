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

    def get_token_balance(self, token_id: str) -> float | None:
        """Fetch actual conditional token balance (shares held).
        Returns None if the balance could not be fetched (API error)."""
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
            return None

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

        # Snapshot pre-trade token balance so verification can detect only
        # NET NEW tokens (avoids false positives from pre-existing balance).
        pre_balance = self.get_token_balance(token_id)

        # Guard: if a significant token balance already exists with no tracked
        # position, a previous failed buy likely settled on-chain.  Open the
        # position instead of placing another order (avoids double exposure).
        if pre_balance is not None and pre_balance >= config.SELL_FILLED_BALANCE_THRESHOLD:
            logger.warning(
                f"BUY SKIPPED: pre-existing token balance={pre_balance:.6f} "
                f"detected with no open position — previous buy likely settled. "
                f"Opening position instead of placing duplicate order."
            )
            state.open_position(token_id, direction, worst_price, pre_balance)
            balance = self.get_usdc_balance()
            log_trade(
                "BUY",
                f"{direction} (settled from prior attempt) @ ~${worst_price:.4f} | "
                f"shares={pre_balance:.4f}",
                balance=balance,
            )
            return True

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
                    shares = self.get_token_balance(token_id) or 0.0
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

            # Verify on-chain: order may have filled despite non-MATCHED status
            if self._verify_buy_filled(state, token_id, direction, worst_price, amount, status, pre_balance):
                return True

            # Reset cooldown AFTER verification so the window starts fresh.
            # (Verification takes ~FILL_VERIFY_DELAY * FILL_VERIFY_RETRIES
            # seconds; setting cooldown before would expire during that wait.)
            state.buy_blocked_until = time.time() + config.BUY_REJECT_COOLDOWN
            return False

        except Exception as e:
            logger.error(f"BUY order error: {e}")

            # Verify on-chain: order may have filled despite the error
            if self._verify_buy_filled(state, token_id, direction, worst_price, amount, f"error", pre_balance):
                return True

            # Reset cooldown AFTER verification (same reasoning as rejection path)
            state.buy_blocked_until = time.time() + config.BUY_REJECT_COOLDOWN
            return False
        finally:
            state.buy_in_flight = False

    def _verify_buy_filled(
        self,
        state: BotState,
        token_id: str,
        direction: str,
        worst_price: float,
        amount: float,
        status_info: str,
        pre_balance: float | None = None,
    ) -> bool:
        """Check on-chain token balance after a failed buy.

        Waits before checking to allow chain settlement, retrying up to
        FILL_VERIFY_RETRIES times.  Compares against *pre_balance* (the
        balance snapshot taken before the order) so that pre-existing
        tokens are not mistaken for a new fill.
        """
        baseline = pre_balance if pre_balance is not None else 0.0
        if pre_balance is None:
            logger.debug("Buy verify: no pre-balance snapshot, using baseline=0.0")
        try:
            for attempt in range(config.FILL_VERIFY_RETRIES):
                time.sleep(config.FILL_VERIFY_DELAY)
                actual_balance = self.get_token_balance(token_id)
                if actual_balance is None:
                    logger.debug(
                        f"Buy verify: balance API error on attempt {attempt + 1}"
                    )
                    break
                net_new = actual_balance - baseline
                if net_new >= config.SELL_FILLED_BALANCE_THRESHOLD:
                    fill_price = worst_price  # best estimate without response data
                    shares = actual_balance  # track full balance for correct selling
                    logger.warning(
                        f"BUY ACTUALLY FILLED (attempt {attempt + 1}): "
                        f"token balance={actual_balance:.6f} "
                        f"(pre={baseline:.6f} net={net_new:.6f}) "
                        f"despite status={status_info} — opening position"
                    )
                    state.open_position(token_id, direction, fill_price, shares)
                    balance = self.get_usdc_balance()
                    log_trade(
                        "BUY",
                        f"{direction} ~${amount:.2f} @ ~${fill_price:.4f} | "
                        f"shares={shares:.4f} | "
                        f"(detected via balance check, status was {status_info})",
                        balance=balance,
                    )
                    return True
                logger.debug(
                    f"Buy verify attempt {attempt + 1}/{config.FILL_VERIFY_RETRIES}: "
                    f"balance={actual_balance:.6f} net_new={net_new:.6f} < threshold"
                )
        except Exception as verify_err:
            logger.debug(f"Post-buy balance verification failed: {verify_err}")
        return False

    def _verify_sell_filled(self, state: BotState, reason: str, pre_balance: float | None = None) -> bool:
        """Check on-chain token balance after a failed sell.

        Waits before checking to allow chain settlement, retrying up to
        FILL_VERIFY_RETRIES times.  If pre_balance was already below the
        threshold (tokens were gone before we tried to sell), skip
        verification to avoid false-positive closures.
        """
        pos = state.position
        if pos is None:
            return False

        # If we know the balance was already empty before the sell order,
        # a post-sell balance of 0 tells us nothing new — skip.
        if pre_balance is not None and pre_balance < config.SELL_FILLED_BALANCE_THRESHOLD:
            logger.debug(
                f"Sell verify skipped: pre_balance={pre_balance:.6f} "
                f"already below threshold"
            )
            return False

        try:
            for attempt in range(config.FILL_VERIFY_RETRIES):
                time.sleep(config.FILL_VERIFY_DELAY)
                actual_balance = self.get_token_balance(pos.token_id)
                if actual_balance is None:
                    logger.debug(
                        f"Sell verify: balance API error on attempt {attempt + 1}"
                    )
                    break
                if actual_balance < config.SELL_FILLED_BALANCE_THRESHOLD:
                    exit_price = state.best_bid_for(pos.side) or pos.entry_price
                    pnl = (exit_price - pos.entry_price) * pos.shares
                    state.close_position(exit_price, reason)
                    balance = self.get_usdc_balance()
                    logger.warning(
                        f"SELL ACTUALLY FILLED (attempt {attempt + 1}): "
                        f"token balance={actual_balance:.6f} — "
                        f"closing position (detected via post-sell verification)"
                    )
                    log_trade(
                        reason,
                        f"{pos.side} exit (detected via balance check) @ ~${exit_price:.4f} | "
                        f"entry=${pos.entry_price:.4f} | shares={pos.shares:.2f}",
                        pnl=pnl,
                        balance=balance,
                    )
                    return True
                logger.debug(
                    f"Sell verify attempt {attempt + 1}/{config.FILL_VERIFY_RETRIES}: "
                    f"balance={actual_balance:.6f} >= threshold, sell not yet confirmed"
                )
        except Exception as verify_err:
            logger.debug(f"Post-sell balance verification failed: {verify_err}")
        return False

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

        # ── Verify on-chain balance before retrying or giving up ─────────
        # A previous sell attempt may have actually filled even though
        # the API response did not return MATCHED.  If our token balance
        # is effectively zero, close the position immediately.
        if total_attempts > 0:
            actual_balance = self.get_token_balance(pos.token_id)
            if actual_balance is not None and actual_balance < config.SELL_FILLED_BALANCE_THRESHOLD:
                exit_price = state.best_bid_for(pos.side) or pos.entry_price
                pnl = (exit_price - pos.entry_price) * pos.shares
                state.close_position(exit_price, reason)
                balance = self.get_usdc_balance()
                logger.warning(
                    f"SELL ALREADY FILLED: token balance={actual_balance:.6f} — "
                    f"previous attempt succeeded. Closing position."
                )
                log_trade(
                    reason,
                    f"{pos.side} exit (detected via balance check) @ ~${exit_price:.4f} | "
                    f"entry=${pos.entry_price:.4f} | shares={pos.shares:.2f} | "
                    f"attempt={total_attempts}",
                    pnl=pnl,
                    balance=balance,
                )
                return True

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
        sell_shares = actual_balance if (actual_balance is not None and actual_balance > 0) else pos.shares

        logger.info(
            f"SELLING ({reason}) {pos.side}: {sell_shares:.4f} shares "
            f"(stored={pos.shares:.4f} actual={'N/A' if actual_balance is None else f'{actual_balance:.4f}'}) | "
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

            # Verify on-chain: sell may have filled despite non-MATCHED status
            if self._verify_sell_filled(state, reason, pre_balance=actual_balance):
                return True

            if use_fak:
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

            # Verify on-chain: sell may have filled despite the error
            if self._verify_sell_filled(state, reason, pre_balance=actual_balance):
                return True

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
