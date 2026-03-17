"""
Adaptive Volatility Tracker

Tracks per-candle BTC momentum and computes a rolling EMA-based
volatility multiplier that scales all strategy thresholds up or down
to adapt to live market conditions.

High volatility  → multiplier > 1 → stricter momentum entry, wider TP/SL
Low volatility   → multiplier < 1 → easier momentum entry, tighter TP/SL
"""

import logging
from collections import deque

import config

logger = logging.getLogger("volatility")


class VolatilityTracker:
    def __init__(self):
        self._candle_momentums: deque[float] = deque(maxlen=config.ADAPTIVE_LOOKBACK)
        self._ema: float | None = None
        self._last_multiplier: float = 1.0  # cached for logging

    # ── Recording ─────────────────────────────────────────────────────────

    def record_candle(self, abs_momentum: float):
        """
        Called when a 5-min candle completes. Records the absolute BTC
        momentum (|btc_current - btc_candle_open|) for volatility tracking.
        """
        if abs_momentum <= 0:
            return  # skip zero-movement candles (likely no data)

        self._candle_momentums.append(abs_momentum)

        # Update EMA
        alpha = config.ADAPTIVE_EMA_ALPHA
        if self._ema is None:
            self._ema = abs_momentum
        else:
            self._ema = alpha * abs_momentum + (1 - alpha) * self._ema

        new_mult = self.get_multiplier()
        # Log regime changes (>10% shift)
        if abs(new_mult - self._last_multiplier) > 0.1:
            regime = self._regime_label(new_mult)
            logger.info(
                f"VOLATILITY REGIME SHIFT: {self._regime_label(self._last_multiplier)} → "
                f"{regime} | multiplier={new_mult:.2f} | "
                f"ema=${self._ema:.1f} | candles={len(self._candle_momentums)} | "
                f"last_mom=${abs_momentum:.1f}"
            )
        self._last_multiplier = new_mult

    @property
    def candle_count(self) -> int:
        return len(self._candle_momentums)

    @property
    def ema(self) -> float | None:
        return self._ema

    # ── Multiplier ────────────────────────────────────────────────────────

    def get_multiplier(self) -> float:
        """
        Returns the volatility multiplier (clamped).
        Returns 1.0 if adaptive mode is disabled or insufficient data.
        """
        if not config.ADAPTIVE_ENABLED:
            return 1.0

        if self._ema is None or len(self._candle_momentums) < config.ADAPTIVE_MIN_CANDLES:
            return 1.0  # cold-start fallback

        raw = self._ema / config.ADAPTIVE_BASELINE
        return max(config.ADAPTIVE_MIN_MULTIPLIER,
                   min(config.ADAPTIVE_MAX_MULTIPLIER, raw))

    # ── Adaptive Threshold Helpers ────────────────────────────────────────

    def adaptive_momentum(self, base_value: float) -> float:
        """Scale a momentum threshold by the volatility multiplier."""
        return base_value * self.get_multiplier()

    def adaptive_tp(self, base_tp: float) -> float:
        """
        Scale take-profit. Higher vol → TP closer to 1.0 (let winners run).
        Formula: 1.0 - (1.0 - base_tp) / sqrt(multiplier)
        """
        mult = self.get_multiplier()
        gap = 1.0 - base_tp  # e.g. 0.08 for TP=0.92
        return min(0.98, 1.0 - gap / (mult ** 0.5))

    def adaptive_sl(self, base_sl: float) -> float:
        """
        Scale stop-loss. Higher vol → SL further from entry (more room).
        Formula: base_sl / sqrt(multiplier)
        """
        mult = self.get_multiplier()
        return max(0.15, base_sl / (mult ** 0.5))

    def adaptive_spread(self, base_spread: float) -> float:
        """Scale max spread tolerance by multiplier (linear)."""
        mult = self.get_multiplier()
        return base_spread * mult

    def adaptive_entry_price_max(self, base_max: float) -> float:
        """
        Scale entry price max. Higher vol → willing to pay a bit more.
        Shift is conservative: half the multiplier effect.
        """
        mult = self.get_multiplier()
        shift = (mult - 1.0) * 0.03  # ~3 cents per 1x multiplier
        return min(0.85, base_max + shift)

    # ── Display / Logging ─────────────────────────────────────────────────

    def status_str(self) -> str:
        """Short status string for periodic logging."""
        mult = self.get_multiplier()
        regime = self._regime_label(mult)
        ema_str = f"${self._ema:.1f}" if self._ema else "N/A"
        return (
            f"vol={regime}({mult:.2f}) ema={ema_str} "
            f"n={len(self._candle_momentums)}"
        )

    @staticmethod
    def _regime_label(multiplier: float) -> str:
        if multiplier <= 0.7:
            return "LOW"
        elif multiplier <= 1.3:
            return "NORMAL"
        elif multiplier <= 2.0:
            return "HIGH"
        else:
            return "EXTREME"
