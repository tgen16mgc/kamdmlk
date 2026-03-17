import logging
import time
from dataclasses import dataclass, field

from volatility_tracker import VolatilityTracker

logger = logging.getLogger("state")


@dataclass
class Position:
    token_id: str
    side: str  # "Up" or "Down"
    entry_price: float
    shares: float
    entry_time: float
    market_condition_id: str


@dataclass
class TradeRecord:
    timestamp: float
    market_condition_id: str
    direction: str  # "Up" or "Down"
    entry_price: float
    exit_price: float
    shares: float
    pnl: float
    exit_reason: str  # "TP", "SL", "TIME", "RESOLVED"


class BotState:
    def __init__(self):
        self.position: Position | None = None
        self.trades: list[TradeRecord] = []
        self.consecutive_losses: int = 0
        self.cooldown_remaining: int = 0
        self.session_pnl: float = 0.0
        self.wins: int = 0
        self.losses: int = 0
        self.starting_balance: float = 0.0
        self.session_stopped: bool = False

        # Adaptive volatility tracking
        self.volatility = VolatilityTracker()

        # Current market info
        self.current_market_id: str | None = None
        self.current_condition_id: str | None = None
        self.up_token_id: str | None = None
        self.down_token_id: str | None = None
        self.market_end_time: float | None = None
        self.market_neg_risk: bool = False
        self.market_tick_size: str = "0.01"

        # BTC price tracking
        self.btc_candle_open: float | None = None
        self.btc_current: float | None = None
        self.btc_prices: list[tuple[float, float]] = []  # (timestamp, price)

        # Market token prices (from WebSocket or polling), per direction
        self.up_best_bid: float | None = None
        self.up_best_ask: float | None = None
        self.up_last_trade: float | None = None
        self.down_best_bid: float | None = None
        self.down_best_ask: float | None = None
        self.down_last_trade: float | None = None

        # Operational
        self.running: bool = True
        self.market_resolved: bool = False
        self.winning_token_id: str | None = None

        # Order rejection tracking
        self.buy_blocked_until: float = 0.0  # timestamp: don't buy until this time
        self.buy_in_flight: bool = False      # True while a buy order is being placed (prevents double-entry)
        self.sell_pending: bool = False       # True when we want to sell but haven't succeeded
        self.sell_attempts: int = 0           # consecutive sell failures this position
        self.sell_reason: str = ""            # why we're trying to sell (TP/SL/TIME)

    def has_position(self) -> bool:
        return self.position is not None

    def best_bid_for(self, direction: str) -> float | None:
        return self.up_best_bid if direction == "Up" else self.down_best_bid

    def best_ask_for(self, direction: str) -> float | None:
        return self.up_best_ask if direction == "Up" else self.down_best_ask

    def last_trade_for(self, direction: str) -> float | None:
        return self.up_last_trade if direction == "Up" else self.down_last_trade

    def time_remaining(self) -> float | None:
        if self.market_end_time is None:
            return None
        return max(0, self.market_end_time - time.time())

    def btc_momentum(self) -> float | None:
        if self.btc_candle_open is None or self.btc_current is None:
            return None
        return self.btc_current - self.btc_candle_open

    def btc_velocity(self, window_seconds: float = 15.0) -> float | None:
        """Average BTC price change per second over the last `window_seconds`."""
        now = time.time()
        cutoff = now - window_seconds
        recent = [(t, p) for t, p in self.btc_prices if t >= cutoff]
        if len(recent) < 2:
            return None
        first_t, first_p = recent[0]
        last_t, last_p = recent[-1]
        dt = last_t - first_t
        if dt <= 0:
            return None
        return (last_p - first_p) / dt

    def record_btc_price(self, price: float):
        now = time.time()
        self.btc_current = price
        self.btc_prices.append((now, price))
        # Keep only last 120 seconds
        cutoff = now - 120
        self.btc_prices = [(t, p) for t, p in self.btc_prices if t >= cutoff]

    def open_position(self, token_id: str, side: str, price: float, shares: float):
        self.position = Position(
            token_id=token_id,
            side=side,
            entry_price=price,
            shares=shares,
            entry_time=time.time(),
            market_condition_id=self.current_condition_id or "",
        )
        self.sell_pending = False
        self.sell_attempts = 0
        self.sell_reason = ""
        logger.info(
            f"POSITION OPENED: {side} @ ${price:.4f} | "
            f"{shares:.2f} shares | token={token_id[:16]}..."
        )

    def mark_sell_failed(self):
        self.sell_attempts += 1
        logger.warning(
            f"SELL FAILED (attempt {self.sell_attempts}) | "
            f"reason={self.sell_reason} | position={self.position.side if self.position else 'N/A'}"
        )

    def close_position(self, exit_price: float, exit_reason: str):
        if self.position is None:
            return
        self.sell_pending = False
        self.sell_attempts = 0
        self.sell_reason = ""
        pnl = (exit_price - self.position.entry_price) * self.position.shares
        record = TradeRecord(
            timestamp=time.time(),
            market_condition_id=self.position.market_condition_id,
            direction=self.position.side,
            entry_price=self.position.entry_price,
            exit_price=exit_price,
            shares=self.position.shares,
            pnl=pnl,
            exit_reason=exit_reason,
        )
        self.trades.append(record)
        self.session_pnl += pnl

        if pnl >= 0:
            self.wins += 1
            self.consecutive_losses = 0
        else:
            self.losses += 1
            self.consecutive_losses += 1

        logger.info(
            f"POSITION CLOSED: {exit_reason} | {self.position.side} "
            f"entry=${self.position.entry_price:.4f} exit=${exit_price:.4f} "
            f"PnL=${pnl:+.4f} | Session=${self.session_pnl:+.4f} "
            f"W/L={self.wins}/{self.losses}"
        )
        self.position = None

    def check_session_stop(self, stop_pct: float) -> bool:
        """Return True if session losses exceed the stop-loss threshold."""
        if self.starting_balance <= 0 or self.session_stopped:
            return self.session_stopped
        loss = -self.session_pnl
        if loss > 0 and loss >= self.starting_balance * stop_pct:
            if not self.session_stopped:
                self.session_stopped = True
                logger.warning(
                    f"SESSION STOP-LOSS HIT: lost ${loss:.2f} "
                    f"({loss / self.starting_balance * 100:.0f}% of ${self.starting_balance:.2f}). "
                    f"No more trades this session."
                )
            return True
        return False

    def is_in_cooldown(self, loss_limit: int, cooldown_rounds: int) -> bool:
        if self.cooldown_remaining > 0:
            return True
        if self.consecutive_losses >= loss_limit:
            self.cooldown_remaining = cooldown_rounds
            logger.warning(
                f"COOLDOWN ACTIVATED: {self.consecutive_losses} consecutive losses, "
                f"skipping {cooldown_rounds} round(s)"
            )
            return True
        return False

    def consume_cooldown(self):
        if self.cooldown_remaining > 0:
            self.cooldown_remaining -= 1
            if self.cooldown_remaining == 0:
                self.consecutive_losses = 0
                logger.info("COOLDOWN EXPIRED: Ready to trade again")

    def reset_for_new_market(self):
        # Record completed candle's momentum for volatility tracking
        if self.btc_candle_open is not None and self.btc_current is not None:
            abs_mom = abs(self.btc_current - self.btc_candle_open)
            self.volatility.record_candle(abs_mom)

        self.current_market_id = None
        self.current_condition_id = None
        self.up_token_id = None
        self.down_token_id = None
        self.market_end_time = None
        self.market_neg_risk = False
        self.market_tick_size = "0.01"
        self.btc_candle_open = None
        self.up_best_bid = None
        self.up_best_ask = None
        self.up_last_trade = None
        self.down_best_bid = None
        self.down_best_ask = None
        self.down_last_trade = None
        self.market_resolved = False
        self.buy_in_flight = False
        self.winning_token_id = None
        self.sell_pending = False
        self.sell_attempts = 0
        self.sell_reason = ""

    def summary(self) -> str:
        total = self.wins + self.losses
        wr = (self.wins / total * 100) if total > 0 else 0
        return (
            f"Session PnL: ${self.session_pnl:+.4f} | "
            f"Trades: {total} | W/L: {self.wins}/{self.losses} ({wr:.0f}%) | "
            f"Consecutive losses: {self.consecutive_losses}"
        )
