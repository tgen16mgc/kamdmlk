"""
Tests for the post-buy balance verification logic in trader.buy().

Covers the scenario where a buy order appears to fail (API response not MATCHED
or an exception occurs) but actually fills on-chain.  The bot must detect the
non-zero token balance and open the position instead of silently losing money.
"""

import time
import unittest
from unittest.mock import MagicMock, patch

# Patch environment before importing any project modules
with patch.dict("os.environ", {"PRIVATE_KEY": "0x" + "ab" * 32}):
    from state import BotState
    from trader import Trader


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


def _make_state_no_position() -> BotState:
    """Create a BotState ready for a buy attempt (no open position)."""
    state = BotState()
    state.up_token_id = "tok_up_123"
    state.down_token_id = "tok_down_456"
    state.current_condition_id = "cond_abc"
    state.market_end_time = time.time() + 120
    state.up_best_bid = 0.62
    state.down_best_bid = 0.55
    return state


class TestBuyBalanceVerification(unittest.TestCase):
    """Verify that trader.buy() detects an on-chain fill after a rejected order."""

    def test_buy_detects_fill_on_rejection(self):
        """If the API returns a non-MATCHED status but the token balance shows
        tokens were received, buy() should open the position and return True."""
        trader = _FakeTrader()
        state = _make_state_no_position()

        # API says REJECTED, but tokens were actually received
        trader.client.create_market_order.return_value = "mock_order"
        trader.client.post_order.return_value = {"status": "REJECTED", "orderID": "abc"}
        trader._token_balance = 3.0  # tokens on-chain

        result = trader.buy(state, "tok_up_123", "Up", worst_price=0.65)

        self.assertTrue(result, "buy() should return True when fill detected via balance")
        self.assertIsNotNone(state.position, "Position should be opened")
        self.assertEqual(state.position.side, "Up")
        self.assertAlmostEqual(state.position.shares, 3.0)
        self.assertFalse(state.buy_in_flight)

    def test_buy_detects_fill_on_exception(self):
        """If an exception occurs during order placement but the token balance
        shows tokens were received, buy() should open the position."""
        trader = _FakeTrader()
        state = _make_state_no_position()

        # Order placement throws an exception
        trader.client.create_market_order.return_value = "mock_order"
        trader.client.post_order.side_effect = Exception("Network timeout")
        trader._token_balance = 2.5  # tokens on-chain despite the error

        result = trader.buy(state, "tok_up_123", "Up", worst_price=0.65)

        self.assertTrue(result, "buy() should return True when fill detected via balance")
        self.assertIsNotNone(state.position)
        self.assertAlmostEqual(state.position.shares, 2.5)
        self.assertFalse(state.buy_in_flight)

    def test_buy_no_false_positive_on_zero_balance(self):
        """If the buy was truly rejected and no tokens were received,
        buy() should return False and NOT open a position."""
        trader = _FakeTrader()
        state = _make_state_no_position()

        trader.client.create_market_order.return_value = "mock_order"
        trader.client.post_order.return_value = {"status": "REJECTED", "orderID": "abc"}
        trader._token_balance = 0.0  # no tokens received

        result = trader.buy(state, "tok_up_123", "Up", worst_price=0.65)

        self.assertFalse(result, "buy() should return False when no tokens on-chain")
        self.assertIsNone(state.position, "Position should NOT be opened")
        self.assertFalse(state.buy_in_flight)

    def test_buy_no_false_positive_on_api_error(self):
        """If get_token_balance returns None (API error during verification),
        buy() should return False — do not assume fill without evidence."""
        trader = _FakeTrader()
        state = _make_state_no_position()

        trader.client.create_market_order.return_value = "mock_order"
        trader.client.post_order.return_value = {"status": "REJECTED", "orderID": "abc"}
        trader._token_balance = None  # API error during balance check

        result = trader.buy(state, "tok_up_123", "Up", worst_price=0.65)

        self.assertFalse(result, "buy() should return False when balance is unknown")
        self.assertIsNone(state.position, "Position should NOT be opened")

    def test_buy_sets_cooldown_even_when_fill_detected(self):
        """Even when a fill is detected via balance check, the buy cooldown
        should still be set (it was set before the verification)."""
        trader = _FakeTrader()
        state = _make_state_no_position()

        trader.client.create_market_order.return_value = "mock_order"
        trader.client.post_order.return_value = {"status": "REJECTED", "orderID": "abc"}
        trader._token_balance = 3.0

        before = time.time()
        trader.buy(state, "tok_up_123", "Up", worst_price=0.65)

        # Cooldown should have been set (buy_blocked_until > now)
        self.assertGreater(state.buy_blocked_until, before)

    def test_buy_normal_matched_still_works(self):
        """Normal successful buy (MATCHED status) should still work as before."""
        trader = _FakeTrader()
        state = _make_state_no_position()

        trader.client.create_market_order.return_value = "mock_order"
        trader.client.post_order.return_value = {
            "status": "MATCHED",
            "orderID": "abc",
            "averagePrice": "0.62",
            "sizeMatched": "3.2",
        }

        result = trader.buy(state, "tok_up_123", "Up", worst_price=0.65)

        self.assertTrue(result)
        self.assertIsNotNone(state.position)
        self.assertEqual(state.position.side, "Up")
        self.assertAlmostEqual(state.position.entry_price, 0.62)
        self.assertAlmostEqual(state.position.shares, 3.2)

    def test_buy_balance_below_threshold_no_position(self):
        """If token balance is non-zero but below SELL_FILLED_BALANCE_THRESHOLD,
        it should be treated as effectively zero (dust) and not open a position."""
        trader = _FakeTrader()
        state = _make_state_no_position()

        trader.client.create_market_order.return_value = "mock_order"
        trader.client.post_order.return_value = {"status": "REJECTED", "orderID": "abc"}
        trader._token_balance = 0.005  # dust amount below threshold (0.01)

        result = trader.buy(state, "tok_up_123", "Up", worst_price=0.65)

        self.assertFalse(result)
        self.assertIsNone(state.position)


if __name__ == "__main__":
    unittest.main()
