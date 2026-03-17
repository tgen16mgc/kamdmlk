import logging
import time

import config
from state import BotState
from trader import Trader
from logger_setup import log_trade

logger = logging.getLogger("strategy")


class MomentumStrategy:
    def __init__(self, state: BotState, trader: Trader):
        self.state = state
        self.trader = trader
        self._last_log_time = 0

    def tick(self):
        """
        Called every STRATEGY_LOOP_INTERVAL seconds.
        Evaluates entry and exit conditions.
        """
        s = self.state

        if s.market_end_time is None or s.up_token_id is None:
            return

        remaining = s.time_remaining()
        if remaining is None:
            return

        # If market resolved while we have a position, handle it
        if s.market_resolved and s.has_position():
            self._handle_resolved()
            return

        if s.has_position():
            self._check_exits(remaining)
        else:
            self._check_entry(remaining)

        # Periodic status log (every 5 seconds)
        now = time.time()
        if now - self._last_log_time > 5:
            self._log_status(remaining)
            self._last_log_time = now

    def _check_entry(self, remaining: float):
        """Evaluate all entry conditions."""
        s = self.state
        vol = s.volatility  # adaptive volatility tracker

        # Guard: buy already in-flight (prevents double-entry during async order placement)
        if s.buy_in_flight:
            return

        # Session stop-loss: no new trades if session loss threshold hit
        if s.check_session_stop(config.SESSION_STOP_LOSS_PCT):
            return

        # Condition 1: Time window (2:40 to 0:50 remaining)
        if remaining > config.ENTRY_WINDOW_MAX or remaining < config.ENTRY_WINDOW_MIN:
            return

        # Condition 2: Buy cooldown after rejection
        if time.time() < s.buy_blocked_until:
            return

        # Condition 3: Cooldown check (consecutive losses)
        if s.is_in_cooldown(config.CONSECUTIVE_LOSS_LIMIT, config.COOLDOWN_ROUNDS):
            return

        # Condition 4: BTC momentum (adaptive thresholds)
        momentum = s.btc_momentum()
        if momentum is None:
            return
        abs_momentum = abs(momentum)
        adaptive_mom_min = vol.adaptive_momentum(config.BTC_MOMENTUM_MIN)
        if abs_momentum < adaptive_mom_min:
            return

        # Determine direction: positive momentum = Up, negative = Down
        direction = "Up" if momentum > 0 else "Down"
        token_id = s.up_token_id if direction == "Up" else s.down_token_id

        # Condition 5: Momentum velocity - BTC still moving in the same direction
        velocity = s.btc_velocity(config.MOMENTUM_VELOCITY_WINDOW)
        if velocity is None:
            return
        if direction == "Up" and velocity <= 0:
            return
        if direction == "Down" and velocity >= 0:
            return

        # Condition 6: Entry price range (3-tier with adaptive scaling)
        adaptive_mom_med = vol.adaptive_momentum(config.BTC_MOMENTUM_MED)
        adaptive_mom_high = vol.adaptive_momentum(config.BTC_MOMENTUM_HIGH)

        if abs_momentum >= adaptive_mom_high:
            entry_min = config.ENTRY_PRICE_MIN_HIGH_MOM
            entry_max = vol.adaptive_entry_price_max(config.ENTRY_PRICE_MAX_HIGH_MOM)
        elif abs_momentum >= adaptive_mom_med:
            entry_min = config.ENTRY_PRICE_MIN
            entry_max = vol.adaptive_entry_price_max(config.ENTRY_PRICE_MAX)
        else:
            entry_min = config.ENTRY_PRICE_MIN_LOW_MOM
            entry_max = vol.adaptive_entry_price_max(config.ENTRY_PRICE_MAX_LOW_MOM)

        token_price = s.best_ask_for(direction) or s.last_trade_for(direction)
        if token_price is None:
            return

        if token_price < entry_min or token_price > entry_max:
            return

        # Condition 7: Spread guard (adaptive)
        adaptive_spread = vol.adaptive_spread(config.MAX_SPREAD)
        dir_bid = s.best_bid_for(direction)
        dir_ask = s.best_ask_for(direction)
        if dir_bid is not None and dir_ask is not None:
            spread = dir_ask - dir_bid
            if spread > adaptive_spread:
                logger.debug(f"Spread too wide: ${spread:.4f} > ${adaptive_spread:.4f}")
                return

        # ALL CONDITIONS MET - ENTER
        mult = vol.get_multiplier()
        logger.info(
            f"ENTRY SIGNAL: {direction} | "
            f"BTC momentum=${momentum:+.2f} velocity={velocity:+.4f}/s | "
            f"token_price=${token_price:.4f} | "
            f"remaining={remaining:.0f}s | "
            f"vol_mult={mult:.2f} adaptive_mom_min=${adaptive_mom_min:.1f}"
        )

        success = self.trader.buy(s, token_id, direction, worst_price=entry_max)
        if not success:
            # buy_blocked_until is already set by trader.buy() on rejection
            cooldown = max(0, s.buy_blocked_until - time.time())
            logger.info(
                f"BUY rejected — cooling down {cooldown:.0f}s before next attempt"
            )

    def _check_exits(self, remaining: float):
        """Evaluate exit conditions for the current position."""
        s = self.state
        pos = s.position
        vol = s.volatility  # adaptive volatility tracker

        # If a sell is already pending (previous attempt failed), keep retrying
        if s.sell_pending:
            max_total = config.SELL_MAX_RETRIES + config.SELL_FAK_ATTEMPTS
            if s.sell_attempts >= max_total:
                # We've exhausted all retries — verify on-chain balance in case
                # a previous sell actually filled (the API may not have reported it).
                # trader.sell() will detect zero balance and close the position.
                self.trader.sell(s, s.sell_reason)
                return
            self.trader.sell(s, s.sell_reason)
            return

        # Use best bid for exit valuation (what we can actually sell at)
        direction = pos.side
        current_price = s.best_bid_for(direction) or s.last_trade_for(direction)
        if current_price is None:
            if remaining <= config.TIME_STOP_SECONDS:
                logger.warning("TIME STOP triggered (no price data)")
                self.trader.sell(s, "TIME")
            return

        # Adaptive TP / SL thresholds
        adaptive_tp = vol.adaptive_tp(config.TAKE_PROFIT)
        adaptive_sl = vol.adaptive_sl(config.STOP_LOSS)
        adaptive_mom_min = vol.adaptive_momentum(config.BTC_MOMENTUM_MIN)

        # Exit 1: Take Profit (adaptive)
        if current_price >= adaptive_tp:
            logger.info(f"TAKE PROFIT: price=${current_price:.4f} >= ${adaptive_tp:.4f} (base=${config.TAKE_PROFIT})")
            self.trader.sell(s, "TP")
            return

        # Exit 2: Stop Loss (adaptive)
        if current_price <= adaptive_sl:
            logger.info(f"STOP LOSS: price=${current_price:.4f} <= ${adaptive_sl:.4f} (base=${config.STOP_LOSS})")
            self.trader.sell(s, "SL")
            return

        # Exit 3: Breakeven early time stop — cut dead trades, but NOT if momentum still supports us
        if remaining <= config.BREAKEVEN_TIME_STOP_SECONDS and current_price <= pos.entry_price:
            momentum = s.btc_momentum()
            momentum_supports = False
            if momentum is not None:
                if pos.side == "Up" and momentum >= adaptive_mom_min:
                    momentum_supports = True
                elif pos.side == "Down" and momentum <= -adaptive_mom_min:
                    momentum_supports = True

            if momentum_supports:
                logger.info(
                    f"BREAKEVEN SKIP: {remaining:.0f}s left | "
                    f"price=${current_price:.4f} <= entry but "
                    f"momentum=${momentum:+.2f} still in our favor — holding"
                )
            else:
                logger.info(
                    f"BREAKEVEN TIME STOP: {remaining:.0f}s left | "
                    f"price=${current_price:.4f} <= entry=${pos.entry_price:.4f} | "
                    f"momentum={'N/A' if momentum is None else f'${momentum:+.2f}'}"
                )
                self.trader.sell(s, "TIME")
                return

        # Exit 4: Time Stop (with momentum override down to HARD_TIME_STOP)
        if remaining <= config.TIME_STOP_SECONDS:
            if remaining > config.HARD_TIME_STOP_SECONDS:
                momentum = s.btc_momentum()
                momentum_supports = False
                if momentum is not None:
                    if pos.side == "Up" and momentum >= adaptive_mom_min:
                        momentum_supports = True
                    elif pos.side == "Down" and momentum <= -adaptive_mom_min:
                        momentum_supports = True

                if momentum_supports:
                    logger.info(
                        f"TIME STOP HOLD: {remaining:.0f}s left | "
                        f"price=${current_price:.4f} | "
                        f"momentum=${momentum:+.2f} still in our favor — "
                        f"holding until {config.HARD_TIME_STOP_SECONDS}s"
                    )
                    return

            logger.info(
                f"TIME STOP: {remaining:.0f}s remaining | price=${current_price:.4f}"
            )
            self.trader.sell(s, "TIME")
            return

    def _handle_resolved(self):
        """Handle position when market resolves (works even if sells failed)."""
        s = self.state
        pos = s.position
        if pos is None:
            return

        if s.sell_pending:
            logger.info(
                f"Market resolved while sell was pending "
                f"(after {s.sell_attempts} failed attempts)"
            )

        if s.winning_token_id == pos.token_id:
            pnl = (1.0 - pos.entry_price) * pos.shares
            s.close_position(1.0, "RESOLVED")
            log_trade("WIN", f"Market resolved in our favor! Token redeems at $1.00",
                      pnl=pnl, balance=self.trader.get_usdc_balance())
        else:
            pnl = (0.0 - pos.entry_price) * pos.shares
            s.close_position(0.0, "RESOLVED")
            log_trade("LOSS", f"Market resolved against us. Token worth $0.00",
                      pnl=pnl, balance=self.trader.get_usdc_balance())

    def _log_status(self, remaining: float):
        """Periodic status log."""
        s = self.state
        momentum = s.btc_momentum()
        velocity = s.btc_velocity(config.MOMENTUM_VELOCITY_WINDOW)

        parts = [f"t-{remaining:.0f}s"]

        if s.btc_current is not None:
            parts.append(f"BTC=${s.btc_current:,.2f}")
        if momentum is not None:
            parts.append(f"mom=${momentum:+.2f}")
        if velocity is not None:
            parts.append(f"vel={velocity:+.2f}/s")

        # Adaptive volatility status
        parts.append(s.volatility.status_str())

        if s.up_best_bid is not None:
            parts.append(f"UP={s.up_best_bid:.2f}/{s.up_best_ask:.2f}" if s.up_best_ask else f"UP_bid={s.up_best_bid:.2f}")
        if s.down_best_bid is not None:
            parts.append(f"DN={s.down_best_bid:.2f}/{s.down_best_ask:.2f}" if s.down_best_ask else f"DN_bid={s.down_best_bid:.2f}")

        if s.has_position():
            pos = s.position
            current = s.best_bid_for(pos.side) or s.last_trade_for(pos.side) or pos.entry_price
            unrealized = (current - pos.entry_price) * pos.shares
            parts.append(f"POS={pos.side} unrealized=${unrealized:+.4f}")
            if s.sell_pending:
                parts.append(f"SELL_PENDING(attempt={s.sell_attempts})")
        elif time.time() < s.buy_blocked_until:
            wait = s.buy_blocked_until - time.time()
            parts.append(f"BUY_COOLDOWN({wait:.1f}s)")

        logger.info(" | ".join(parts))
