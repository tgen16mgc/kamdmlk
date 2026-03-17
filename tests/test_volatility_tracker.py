"""
Tests for VolatilityTracker — adaptive volatility regime detection.

Verifies EMA calculation, multiplier clamping, cold-start fallback,
and scaling of momentum / TP / SL / spread thresholds.
"""

import unittest
from unittest.mock import patch


# Patch config before importing VolatilityTracker
_CONFIG_OVERRIDES = {
    "ADAPTIVE_ENABLED": True,
    "ADAPTIVE_BASELINE": 45.0,
    "ADAPTIVE_LOOKBACK": 20,
    "ADAPTIVE_EMA_ALPHA": 0.3,
    "ADAPTIVE_MIN_MULTIPLIER": 0.5,
    "ADAPTIVE_MAX_MULTIPLIER": 3.0,
    "ADAPTIVE_MIN_CANDLES": 3,
}


def _patch_config():
    """Return a context manager that patches config module attributes."""
    import config
    return patch.multiple(config, **_CONFIG_OVERRIDES)


class TestVolatilityTracker(unittest.TestCase):

    def _make_tracker(self):
        from volatility_tracker import VolatilityTracker
        return VolatilityTracker()

    # ── Cold-start / Warmup ─────────────────────────────────────────────

    def test_cold_start_returns_1(self):
        """With no candles recorded, multiplier should be 1.0 (no adaptation)."""
        with _patch_config():
            vt = self._make_tracker()
            self.assertEqual(vt.get_multiplier(), 1.0)

    def test_insufficient_candles_returns_1(self):
        """With fewer than ADAPTIVE_MIN_CANDLES, multiplier should be 1.0."""
        with _patch_config():
            vt = self._make_tracker()
            vt.record_candle(50.0)
            vt.record_candle(40.0)
            # Only 2 candles, need 3
            self.assertEqual(vt.get_multiplier(), 1.0)

    def test_warmup_threshold(self):
        """Exactly ADAPTIVE_MIN_CANDLES should activate adaptation."""
        with _patch_config():
            vt = self._make_tracker()
            vt.record_candle(45.0)
            vt.record_candle(45.0)
            vt.record_candle(45.0)
            # 3 candles, baseline=45 → mult ≈ 1.0
            self.assertAlmostEqual(vt.get_multiplier(), 1.0, delta=0.1)

    # ── EMA Calculation ─────────────────────────────────────────────────

    def test_ema_first_candle(self):
        """First candle sets EMA to candle value."""
        with _patch_config():
            vt = self._make_tracker()
            vt.record_candle(60.0)
            self.assertAlmostEqual(vt.ema, 60.0)

    def test_ema_updates_correctly(self):
        """EMA should follow: ema = alpha * new + (1 - alpha) * old."""
        with _patch_config():
            vt = self._make_tracker()
            alpha = 0.3

            vt.record_candle(50.0)  # ema = 50
            self.assertAlmostEqual(vt.ema, 50.0)

            vt.record_candle(80.0)  # ema = 0.3*80 + 0.7*50 = 59
            self.assertAlmostEqual(vt.ema, 59.0)

            vt.record_candle(40.0)  # ema = 0.3*40 + 0.7*59 = 53.3
            self.assertAlmostEqual(vt.ema, 53.3)

    # ── Multiplier Clamping ─────────────────────────────────────────────

    def test_multiplier_normal(self):
        """Normal volatility should produce multiplier near 1.0."""
        with _patch_config():
            vt = self._make_tracker()
            for _ in range(5):
                vt.record_candle(45.0)  # exactly baseline
            self.assertAlmostEqual(vt.get_multiplier(), 1.0, delta=0.05)

    def test_multiplier_high_vol(self):
        """High volatility should produce multiplier > 1."""
        with _patch_config():
            vt = self._make_tracker()
            for _ in range(5):
                vt.record_candle(120.0)
            mult = vt.get_multiplier()
            self.assertGreater(mult, 1.5)

    def test_multiplier_clamped_at_max(self):
        """Extreme volatility should be clamped at ADAPTIVE_MAX_MULTIPLIER."""
        with _patch_config():
            vt = self._make_tracker()
            for _ in range(5):
                vt.record_candle(500.0)  # absurdly high
            self.assertEqual(vt.get_multiplier(), 3.0)

    def test_multiplier_clamped_at_min(self):
        """Very low volatility should be clamped at ADAPTIVE_MIN_MULTIPLIER."""
        with _patch_config():
            vt = self._make_tracker()
            for _ in range(5):
                vt.record_candle(5.0)  # very low
            self.assertEqual(vt.get_multiplier(), 0.5)

    def test_multiplier_disabled(self):
        """When ADAPTIVE_ENABLED=False, always return 1.0."""
        import config
        with patch.multiple(config, **{**_CONFIG_OVERRIDES, "ADAPTIVE_ENABLED": False}):
            vt = self._make_tracker()
            for _ in range(5):
                vt.record_candle(120.0)
            self.assertEqual(vt.get_multiplier(), 1.0)

    # ── Adaptive Threshold Scaling ──────────────────────────────────────

    def test_adaptive_momentum(self):
        """Momentum threshold should scale with multiplier."""
        with _patch_config():
            vt = self._make_tracker()
            for _ in range(5):
                vt.record_candle(90.0)  # 2x baseline → mult=2
            mult = vt.get_multiplier()
            self.assertAlmostEqual(mult, 2.0, delta=0.1)
            adapted = vt.adaptive_momentum(30.0)
            self.assertAlmostEqual(adapted, 30.0 * mult, delta=1.0)

    def test_adaptive_tp_higher_in_high_vol(self):
        """TP should be closer to 1.0 (higher) in high vol."""
        with _patch_config():
            vt = self._make_tracker()
            # Normal vol
            base_tp = 0.92
            for _ in range(5):
                vt.record_candle(45.0)
            tp_normal = vt.adaptive_tp(base_tp)

            # Reset and do high vol
            vt2 = self._make_tracker()
            for _ in range(5):
                vt2.record_candle(120.0)
            tp_high = vt2.adaptive_tp(base_tp)

            self.assertGreater(tp_high, tp_normal)

    def test_adaptive_sl_lower_in_high_vol(self):
        """SL should be lower (more room) in high vol."""
        with _patch_config():
            vt = self._make_tracker()
            base_sl = 0.39
            for _ in range(5):
                vt.record_candle(45.0)
            sl_normal = vt.adaptive_sl(base_sl)

            vt2 = self._make_tracker()
            for _ in range(5):
                vt2.record_candle(120.0)
            sl_high = vt2.adaptive_sl(base_sl)

            self.assertLess(sl_high, sl_normal)

    def test_adaptive_spread_wider_in_high_vol(self):
        """Spread tolerance should increase in high vol."""
        with _patch_config():
            vt = self._make_tracker()
            for _ in range(5):
                vt.record_candle(90.0)
            spread = vt.adaptive_spread(0.05)
            self.assertGreater(spread, 0.05)

    # ── Edge Cases ──────────────────────────────────────────────────────

    def test_zero_momentum_candle_ignored(self):
        """Candles with zero momentum should be ignored."""
        with _patch_config():
            vt = self._make_tracker()
            vt.record_candle(0.0)
            self.assertEqual(vt.candle_count, 0)
            self.assertIsNone(vt.ema)

    def test_negative_momentum_candle_ignored(self):
        """Negative values should be ignored (caller should abs())."""
        with _patch_config():
            vt = self._make_tracker()
            vt.record_candle(-30.0)
            self.assertEqual(vt.candle_count, 0)

    def test_ring_buffer_overflow(self):
        """Ring buffer should cap at ADAPTIVE_LOOKBACK entries."""
        with _patch_config():
            vt = self._make_tracker()
            for i in range(30):  # more than lookback=20
                vt.record_candle(float(40 + i))
            self.assertEqual(vt.candle_count, 20)

    def test_status_str_format(self):
        """status_str() should return a non-empty string."""
        with _patch_config():
            vt = self._make_tracker()
            s = vt.status_str()
            self.assertIn("vol=", s)

            for _ in range(5):
                vt.record_candle(45.0)
            s = vt.status_str()
            self.assertIn("NORMAL", s)


if __name__ == "__main__":
    unittest.main()
