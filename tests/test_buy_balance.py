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

    def test_buy_no_cooldown_when_fill_detected(self, _mock_sleep):
        """When a fill is detected via balance check, no cooldown is needed
        because the buy succeeded (position opened)."""
        trader = _FakeTrader()
        state = _make_state_no_position()

        trader.client.create_market_order.return_value = "mock_order"
        trader.client.post_order.return_value = {"status": "REJECTED", "orderID": "abc"}
        # pre-balance=0, tokens appear in verification
        trader._token_balances = [0.0, 3.0]

        trader.buy(state, "tok_up_123", "Up", worst_price=0.65)

        # Fill was detected → position opened → no cooldown needed
        self.assertIsNotNone(state.position)
        self.assertEqual(state.buy_blocked_until, 0.0)

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

    def test_buy_preflight_guard_opens_position_for_settled_tokens(self, _mock_sleep):
        """If tokens already exist on-chain with no tracked position (e.g. from
        a previous failed buy that settled), buy() should open the position
        immediately without placing another order (prevents double exposure)."""
        trader = _FakeTrader()
        state = _make_state_no_position()

        # Pre-balance shows 3.0 tokens — settled from a prior failed buy
        trader._token_balances = [3.0]

        result = trader.buy(state, "tok_up_123", "Up", worst_price=0.65)

        self.assertTrue(result, "buy() should open position for pre-existing tokens")
        self.assertIsNotNone(state.position, "Position should be opened")
        self.assertAlmostEqual(state.position.shares, 3.0)
        # Must NOT have placed an order (duplicate prevention)
        trader.client.create_market_order.assert_not_called()
        trader.client.post_order.assert_not_called()

    def test_buy_preflight_guard_skips_on_dust(self, _mock_sleep):
        """Pre-flight guard should ignore dust balances below the threshold."""
        trader = _FakeTrader()
        state = _make_state_no_position()

        trader.client.create_market_order.return_value = "mock_order"
        trader.client.post_order.return_value = {"status": "MATCHED", "orderID": "abc",
                                                  "sizeMatched": "2.0"}
        # Pre-balance is dust (below SELL_FILLED_BALANCE_THRESHOLD=0.01)
        trader._token_balances = [0.005]

        result = trader.buy(state, "tok_up_123", "Up", worst_price=0.65)

        self.assertTrue(result)
        # Should have placed the order normally (dust ignored)
        trader.client.post_order.assert_called_once()

    def test_buy_cooldown_set_after_verification_not_before(self, _mock_sleep):
        """buy_blocked_until should be set AFTER verification completes,
        not before, so the cooldown window doesn't expire during verification."""
        trader = _FakeTrader()
        state = _make_state_no_position()

        trader.client.create_market_order.return_value = "mock_order"
        trader.client.post_order.return_value = {"status": "REJECTED", "orderID": "abc"}
        # pre-balance=0, verification also returns 0 (no fill detected)
        trader._token_balances = [0.0, 0.0, 0.0]

        before = time.time()
        trader.buy(state, "tok_up_123", "Up", worst_price=0.65)

        # Cooldown must be set AFTER verification returned, so buy_blocked_until
        # should be > now (which is after the ~2s verification period)
        self.assertGreater(
            state.buy_blocked_until, before,
            "Cooldown should be set after verification, not before"
        )

    def test_buy_preflight_prevents_duplicate_order_scenario(self, _mock_sleep):
        """Full scenario: buy #1 fails, tokens settle, buy #2 detects them
        via pre-flight guard and opens position without placing another order."""
        trader = _FakeTrader()
        state = _make_state_no_position()

        # --- Buy #1: fails, verification doesn't find fill yet ---
        trader.client.create_market_order.return_value = "mock_order"
        trader.client.post_order.return_value = {"status": "REJECTED", "orderID": "abc"}
        trader._token_balances = [0.0, 0.0, 0.0]  # pre=0, verify=0, verify=0

        result1 = trader.buy(state, "tok_up_123", "Up", worst_price=0.65)
        self.assertFalse(result1)
        self.assertIsNone(state.position)

        # --- Buy #1 settles on-chain (tokens appear) ---
        # --- Buy #2: pre-flight guard detects the settled tokens ---
        trader._token_balances = [3.0]  # pre-balance now shows settled tokens
        trader.client.post_order.reset_mock()

        result2 = trader.buy(state, "tok_up_123", "Up", worst_price=0.65)
        self.assertTrue(result2, "buy #2 should detect settled tokens from buy #1")
        self.assertIsNotNone(state.position)
        self.assertAlmostEqual(state.position.shares, 3.0)
        # Crucially: no new order was placed
        trader.client.post_order.assert_not_called()

    def test_buy_exception_uses_longer_cooldown(self, _mock_sleep):
        """After an exception (order may be in-flight), the cooldown should
        use BUY_EXCEPTION_COOLDOWN which is longer than BUY_REJECT_COOLDOWN
        to prevent double orders from settling on-chain."""
        import config
        trader = _FakeTrader()
        state = _make_state_no_position()

        trader.client.create_market_order.return_value = "mock_order"
        trader.client.post_order.side_effect = Exception("Request exception!")
        # pre-balance=0, verification also returns 0 (no fill detected)
        trader._token_balances = [0.0, 0.0, 0.0]

        before = time.time()
        result = trader.buy(state, "tok_up_123", "Up", worst_price=0.65)

        self.assertFalse(result)
        # Cooldown must use the longer BUY_EXCEPTION_COOLDOWN
        expected_min = before + config.BUY_EXCEPTION_COOLDOWN
        self.assertGreaterEqual(
            state.buy_blocked_until, expected_min,
            f"Exception cooldown should be at least {config.BUY_EXCEPTION_COOLDOWN}s, "
            f"got {state.buy_blocked_until - before:.1f}s"
        )

    def test_buy_rejection_uses_short_cooldown(self, _mock_sleep):
        """After a normal rejection (order definitively not accepted), the
        cooldown should use the shorter BUY_REJECT_COOLDOWN."""
        import config
        trader = _FakeTrader()
        state = _make_state_no_position()

        trader.client.create_market_order.return_value = "mock_order"
        trader.client.post_order.return_value = {"status": "REJECTED", "orderID": "abc"}
        # pre-balance=0, verification also returns 0 (no fill detected)
        trader._token_balances = [0.0, 0.0, 0.0]

        before = time.time()
        result = trader.buy(state, "tok_up_123", "Up", worst_price=0.65)

        self.assertFalse(result)
        # Cooldown must use the shorter BUY_REJECT_COOLDOWN (not the longer exception one)
        cooldown_used = state.buy_blocked_until - before
        self.assertLess(
            cooldown_used, config.BUY_EXCEPTION_COOLDOWN,
            "Rejection cooldown should be shorter than exception cooldown"
        )
        self.assertGreaterEqual(
            cooldown_used, config.BUY_REJECT_COOLDOWN,
            f"Rejection cooldown should be at least {config.BUY_REJECT_COOLDOWN}s"
        )


if __name__ == "__main__":
    unittest.main()
