"""
Tests for the sell retry / balance verification logic in trader.sell().

Covers the scenario where a sell order appears to fail (API response not MATCHED)
but actually fills on-chain.  The bot must detect the zero token balance and
close the position instead of retrying or giving up with a stale position.
"""

import time
import unittest
from unittest.mock import MagicMock, patch

# Patch environment before importing any project modules
with patch.dict("os.environ", {"PRIVATE_KEY": "0x" + "ab" * 32}):
    from state import BotState, Position
    from trader import Trader, _parse_status, _parse_fill_price


class _FakeTrader(Trader):
    """Trader subclass that bypasses real CLOB client initialization."""

    def __init__(self):
        super().__init__()
        self.client = MagicMock()
        self._token_balance: float | None = 0.0
        self._usdc_balance: float = 10.0

    # Deterministic balance helpers
    def get_token_balance(self, token_id: str) -> float | None:
        return self._token_balance

    def get_usdc_balance(self) -> float:
        return self._usdc_balance


def _make_state_with_position(
    side: str = "Up",
    entry_price: float = 0.60,
    shares: float = 3.0,
    token_id: str = "tok_up_123",
) -> BotState:
    """Create a BotState that already has an open position."""
    state = BotState()
    state.up_token_id = "tok_up_123"
    state.down_token_id = "tok_down_456"
    state.current_condition_id = "cond_abc"
    state.market_end_time = time.time() + 120
    state.open_position(token_id, side, entry_price, shares)
    # Set a bid so exit_price can be determined
    state.up_best_bid = 0.62
    state.down_best_bid = 0.55
    return state


class TestSellBalanceVerification(unittest.TestCase):
    """Verify that trader.sell() detects a previous fill via on-chain balance."""

    def test_sell_detects_already_filled_on_retry(self):
        """After at least one failed attempt, if token balance is ~0 the
        position should be closed immediately without placing another order."""
        trader = _FakeTrader()
        state = _make_state_with_position()

        # Simulate one prior failed sell attempt
        state.sell_pending = True
        state.sell_attempts = 1
        state.sell_reason = "SL"

        # Token balance is 0 => previous sell actually filled
        trader._token_balance = 0.0

        result = trader.sell(state, "SL")

        self.assertTrue(result, "sell() should return True when position already sold")
        self.assertIsNone(state.position, "Position should be cleared")
        self.assertFalse(state.sell_pending, "sell_pending should be reset")
        self.assertEqual(state.sell_attempts, 0, "sell_attempts should be reset")

    def test_sell_detects_already_filled_after_max_retries(self):
        """Even at the give-up threshold, balance check should detect the fill."""
        trader = _FakeTrader()
        state = _make_state_with_position()

        # Simulate 8 failed attempts (5 FOK + 3 FAK = give_up threshold)
        state.sell_pending = True
        state.sell_attempts = 8
        state.sell_reason = "SL"

        trader._token_balance = 0.0  # already sold on-chain

        result = trader.sell(state, "SL")

        self.assertTrue(result, "sell() should detect fill even at give-up threshold")
        self.assertIsNone(state.position)
        self.assertFalse(state.sell_pending)

    def test_sell_does_not_falsely_close_on_api_error(self):
        """If get_token_balance returns None (API error), do NOT close position.
        Instead, proceed normally with the sell order."""
        trader = _FakeTrader()
        state = _make_state_with_position()

        state.sell_pending = True
        state.sell_attempts = 3
        state.sell_reason = "SL"

        # Simulate API error: balance unknown
        trader._token_balance = None

        # Mock the order placement to return a MATCHED response
        trader.client.create_market_order.return_value = "mock_order"
        trader.client.post_order.return_value = {"status": "MATCHED", "orderID": "abc"}

        result = trader.sell(state, "SL")

        # Should have gone through the normal sell path, not the early-close path
        self.assertTrue(result)
        trader.client.post_order.assert_called_once()

    def test_sell_first_attempt_skips_balance_check(self):
        """On the very first sell attempt (sell_attempts=0), balance check
        should be skipped — we haven't failed yet."""
        trader = _FakeTrader()
        state = _make_state_with_position()

        # First attempt — no prior failures
        state.sell_pending = False
        state.sell_attempts = 0

        trader._token_balance = 0.0  # balance happens to be 0 (e.g. just bought)

        # Mock order placement
        trader.client.create_market_order.return_value = "mock_order"
        trader.client.post_order.return_value = {"status": "MATCHED", "orderID": "abc"}

        result = trader.sell(state, "SL")

        # Should NOT have triggered early close; should have placed the order normally
        trader.client.post_order.assert_called_once()
        self.assertTrue(result)

    def test_sell_with_remaining_balance_continues_normally(self):
        """If token balance is still significant, proceed with the sell normally."""
        trader = _FakeTrader()
        state = _make_state_with_position()

        state.sell_pending = True
        state.sell_attempts = 2
        state.sell_reason = "SL"

        trader._token_balance = 2.5  # still holding tokens

        # Mock order placement — rejected this time
        trader.client.create_market_order.return_value = "mock_order"
        trader.client.post_order.return_value = {"status": "REJECTED", "orderID": "abc"}

        result = trader.sell(state, "SL")

        # Should have tried to sell (not closed early)
        self.assertFalse(result)
        self.assertIsNotNone(state.position, "Position should still exist")
        trader.client.post_order.assert_called_once()


class TestStrategyExitRetry(unittest.TestCase):
    """Test that _check_exits calls trader.sell() even after give-up threshold,
    giving the balance-verification logic a chance to close the position."""

    def test_check_exits_calls_sell_after_give_up(self):
        """After exhausting retries, _check_exits should still call
        trader.sell() so the balance check can detect a filled position."""
        from strategy import MomentumStrategy

        trader = _FakeTrader()
        state = _make_state_with_position()
        strategy = MomentumStrategy(state, trader)

        state.sell_pending = True
        state.sell_attempts = 8  # at give-up threshold
        state.sell_reason = "SL"

        # Balance is 0 => previous sell filled
        trader._token_balance = 0.0

        strategy._check_exits(remaining=60.0)

        # Position should be closed by the balance check in trader.sell()
        self.assertIsNone(state.position)
        self.assertFalse(state.sell_pending)


class TestParseHelpers(unittest.TestCase):
    """Sanity checks for response-parsing helpers."""

    def test_parse_status_matched(self):
        status, oid = _parse_status({"status": "matched", "orderID": "abc123"})
        self.assertEqual(status, "MATCHED")
        self.assertEqual(oid, "abc123")

    def test_parse_status_unknown(self):
        status, oid = _parse_status({})
        self.assertEqual(status, "UNKNOWN")

    def test_parse_fill_price_present(self):
        price = _parse_fill_price({"averagePrice": "0.63"}, 0.60)
        self.assertAlmostEqual(price, 0.63)

    def test_parse_fill_price_fallback(self):
        price = _parse_fill_price({}, 0.60)
        self.assertAlmostEqual(price, 0.60)


if __name__ == "__main__":
    unittest.main()
