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
        self._token_balances: list = []  # if non-empty, consumed in FIFO order
        self._usdc_balance: float = 10.0

    # Deterministic balance helpers
    def get_token_balance(self, token_id: str) -> float | None:
        if self._token_balances:
            return self._token_balances.pop(0)
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


@patch("trader.time.sleep")
class TestBuyBalanceVerification(unittest.TestCase):
    """Verify that trader.buy() detects an on-chain fill after a rejected order."""

    def test_buy_detects_fill_on_rejection(self, _mock_sleep):
        """If the API returns a non-MATCHED status but the token balance shows
        tokens were received, buy() should open the position and return True."""
        trader = _FakeTrader()
        state = _make_state_no_position()

        # API says REJECTED, but tokens were actually received
        trader.client.create_market_order.return_value = "mock_order"
        trader.client.post_order.return_value = {"status": "REJECTED", "orderID": "abc"}
        # pre-balance=0, then tokens appear in verification
        trader._token_balances = [0.0, 3.0]

        result = trader.buy(state, "tok_up_123", "Up", worst_price=0.65)

        self.assertTrue(result, "buy() should return True when fill detected via balance")
        self.assertIsNotNone(state.position, "Position should be opened")
        self.assertEqual(state.position.side, "Up")
        self.assertAlmostEqual(state.position.shares, 3.0)
        self.assertFalse(state.buy_in_flight)

    def test_buy_detects_fill_on_exception(self, _mock_sleep):
        """If an exception occurs during order placement but the token balance
        shows tokens were received, buy() should open the position."""
        trader = _FakeTrader()
        state = _make_state_no_position()

        # Order placement throws an exception
        trader.client.create_market_order.return_value = "mock_order"
        trader.client.post_order.side_effect = Exception("Network timeout")
        # pre-balance=0, then tokens appear in verification
        trader._token_balances = [0.0, 2.5]

        result = trader.buy(state, "tok_up_123", "Up", worst_price=0.65)

        self.assertTrue(result, "buy() should return True when fill detected via balance")
        self.assertIsNotNone(state.position)
        self.assertAlmostEqual(state.position.shares, 2.5)
        self.assertFalse(state.buy_in_flight)

    def test_buy_no_false_positive_on_zero_balance(self, _mock_sleep):
        """If the buy was truly rejected and no tokens were received,
        buy() should return False and NOT open a position."""
        trader = _FakeTrader()
        state = _make_state_no_position()

        trader.client.create_market_order.return_value = "mock_order"
        trader.client.post_order.return_value = {"status": "REJECTED", "orderID": "abc"}
        # pre-balance=0, verification also returns 0 (2 retry attempts)
        trader._token_balances = [0.0, 0.0, 0.0]

        result = trader.buy(state, "tok_up_123", "Up", worst_price=0.65)

        self.assertFalse(result, "buy() should return False when no tokens on-chain")
        self.assertIsNone(state.position, "Position should NOT be opened")
        self.assertFalse(state.buy_in_flight)

    def test_buy_no_false_positive_on_api_error(self, _mock_sleep):
        """If get_token_balance returns None (API error during verification),
        buy() should return False — do not assume fill without evidence."""
        trader = _FakeTrader()
        state = _make_state_no_position()

        trader.client.create_market_order.return_value = "mock_order"
        trader.client.post_order.return_value = {"status": "REJECTED", "orderID": "abc"}
        # pre-balance=None, verification also returns None
        trader._token_balances = [None, None]

        result = trader.buy(state, "tok_up_123", "Up", worst_price=0.65)

        self.assertFalse(result, "buy() should return False when balance is unknown")
        self.assertIsNone(state.position, "Position should NOT be opened")

    def test_buy_sets_cooldown_even_when_fill_detected(self, _mock_sleep):
        """Even when a fill is detected via balance check, the buy cooldown
        should still be set (it was set before the verification)."""
        trader = _FakeTrader()
        state = _make_state_no_position()

        trader.client.create_market_order.return_value = "mock_order"
        trader.client.post_order.return_value = {"status": "REJECTED", "orderID": "abc"}
        # pre-balance=0, tokens appear in verification
        trader._token_balances = [0.0, 3.0]

        before = time.time()
        trader.buy(state, "tok_up_123", "Up", worst_price=0.65)

        # Cooldown should have been set (buy_blocked_until > now)
        self.assertGreater(state.buy_blocked_until, before)

    def test_buy_normal_matched_still_works(self, _mock_sleep):
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

    def test_buy_balance_below_threshold_no_position(self, _mock_sleep):
        """If token balance is non-zero but below SELL_FILLED_BALANCE_THRESHOLD,
        it should be treated as effectively zero (dust) and not open a position."""
        trader = _FakeTrader()
        state = _make_state_no_position()

        trader.client.create_market_order.return_value = "mock_order"
        trader.client.post_order.return_value = {"status": "REJECTED", "orderID": "abc"}
        # pre-balance=0, dust amount in verification (net=0.005 < 0.01 threshold)
        trader._token_balances = [0.0, 0.005, 0.005]

        result = trader.buy(state, "tok_up_123", "Up", worst_price=0.65)

        self.assertFalse(result)
        self.assertIsNone(state.position)

    def test_buy_verify_retries_until_balance_appears(self, mock_sleep):
        """Verification should retry when first check shows zero balance
        but tokens appear on a subsequent check (chain settlement delay)."""
        trader = _FakeTrader()
        state = _make_state_no_position()

        trader.client.create_market_order.return_value = "mock_order"
        trader.client.post_order.return_value = {"status": "REJECTED", "orderID": "abc"}

        # pre=0, verify attempt 1=0 (not yet settled), verify attempt 2=3.0
        balances = iter([0.0, 0.0, 3.0])
        trader.get_token_balance = lambda _tid: next(balances)

        result = trader.buy(state, "tok_up_123", "Up", worst_price=0.65)

        self.assertTrue(result, "buy() should detect fill on second verification attempt")
        self.assertIsNotNone(state.position)
        self.assertAlmostEqual(state.position.shares, 3.0)
        self.assertEqual(mock_sleep.call_count, 2)

    def test_buy_verify_sleeps_before_each_check(self, mock_sleep):
        """Verification must wait FILL_VERIFY_DELAY before each balance check."""
        trader = _FakeTrader()
        state = _make_state_no_position()

        trader.client.create_market_order.return_value = "mock_order"
        trader.client.post_order.return_value = {"status": "REJECTED", "orderID": "abc"}
        # pre-balance=0, tokens appear in verification
        trader._token_balances = [0.0, 3.0]

        trader.buy(state, "tok_up_123", "Up", worst_price=0.65)

        # sleep should have been called at least once with FILL_VERIFY_DELAY
        import config
        mock_sleep.assert_called_with(config.FILL_VERIFY_DELAY)

    def test_buy_no_false_positive_on_preexisting_balance(self, _mock_sleep):
        """If tokens already existed before the buy attempt (e.g. from a
        previous trade or manual deposit), the delta check should prevent
        a false-positive position open."""
        trader = _FakeTrader()
        state = _make_state_no_position()

        trader.client.create_market_order.return_value = "mock_order"
        trader.client.post_order.return_value = {"status": "REJECTED", "orderID": "abc"}
        # Balance was 3.0 before AND after — no new tokens from this order
        trader._token_balances = [3.0, 3.0, 3.0]  # pre-balance + 2 verification attempts

        result = trader.buy(state, "tok_up_123", "Up", worst_price=0.65)

        self.assertFalse(result, "buy() should not open position when no new tokens detected")
        self.assertIsNone(state.position)


if __name__ == "__main__":
    unittest.main()
