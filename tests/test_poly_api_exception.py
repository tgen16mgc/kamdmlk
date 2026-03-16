"""
Tests for PolyApiException-specific handling in trader.buy() and trader.sell().

Covers the scenario where the py-clob-client raises PolyApiException
(status_code=None for network errors, or an HTTP status code for API errors)
instead of a generic Exception.  The bot must use a shorter cooldown for
PolyApiException since the order was almost certainly never accepted.
"""

import time
import unittest
from unittest.mock import MagicMock, patch

# Patch environment before importing any project modules
with patch.dict("os.environ", {"PRIVATE_KEY": "0x" + "ab" * 32}):
    from py_clob_client.exceptions import PolyApiException
    from state import BotState, Position
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
    state.up_best_bid = 0.62
    state.down_best_bid = 0.55
    return state


# ── Buy tests ────────────────────────────────────────────────────────────────

@patch("trader.time.sleep")
class TestBuyPolyApiException(unittest.TestCase):
    """Verify that buy() handles PolyApiException with shorter cooldown."""

    def test_buy_network_error_uses_short_cooldown(self, _mock_sleep):
        """PolyApiException with status_code=None (network failure) should use
        the shorter BUY_REJECT_COOLDOWN since the order never reached the exchange."""
        import config
        trader = _FakeTrader()
        state = _make_state_no_position()

        trader.client.create_market_order.return_value = "mock_order"
        trader.client.post_order.side_effect = PolyApiException(
            error_msg="Request exception!"
        )
        # pre-balance=0, verification also returns 0 (no fill)
        trader._token_balances = [0.0, 0.0, 0.0]

        before = time.time()
        result = trader.buy(state, "tok_up_123", "Up", worst_price=0.65)

        self.assertFalse(result)
        # Should use BUY_REJECT_COOLDOWN, not the longer BUY_EXCEPTION_COOLDOWN
        cooldown_used = state.buy_blocked_until - before
        self.assertLess(
            cooldown_used, config.BUY_EXCEPTION_COOLDOWN,
            "PolyApiException(status_code=None) should use short cooldown, "
            "not the longer exception cooldown"
        )
        self.assertGreaterEqual(
            cooldown_used, config.BUY_REJECT_COOLDOWN,
            f"Cooldown should be at least {config.BUY_REJECT_COOLDOWN}s"
        )
        self.assertFalse(state.buy_in_flight)

    def test_buy_http_error_uses_short_cooldown(self, _mock_sleep):
        """PolyApiException with a status code (e.g. 403) should also use
        the shorter cooldown since the server explicitly rejected the request."""
        import config
        import httpx

        trader = _FakeTrader()
        state = _make_state_no_position()

        trader.client.create_market_order.return_value = "mock_order"
        # Simulate an HTTP 403 response
        mock_resp = MagicMock(spec=httpx.Response)
        mock_resp.status_code = 403
        mock_resp.json.return_value = {"error": "Forbidden"}
        trader.client.post_order.side_effect = PolyApiException(resp=mock_resp)
        # pre-balance=0, verification returns 0
        trader._token_balances = [0.0, 0.0, 0.0]

        before = time.time()
        result = trader.buy(state, "tok_up_123", "Up", worst_price=0.65)

        self.assertFalse(result)
        cooldown_used = state.buy_blocked_until - before
        self.assertLess(
            cooldown_used, config.BUY_EXCEPTION_COOLDOWN,
            "PolyApiException with HTTP status should use short cooldown"
        )
        self.assertFalse(state.buy_in_flight)

    def test_buy_poly_exception_still_verifies_onchain(self, _mock_sleep):
        """Even for PolyApiException, buy() should still verify on-chain
        in case the order was partially sent before the error."""
        trader = _FakeTrader()
        state = _make_state_no_position()

        trader.client.create_market_order.return_value = "mock_order"
        trader.client.post_order.side_effect = PolyApiException(
            error_msg="Request exception!"
        )
        # pre-balance=0, but tokens appear in verification
        trader._token_balances = [0.0, 2.5]

        result = trader.buy(state, "tok_up_123", "Up", worst_price=0.65)

        self.assertTrue(result, "buy() should detect fill even after PolyApiException")
        self.assertIsNotNone(state.position)
        self.assertAlmostEqual(state.position.shares, 2.5)

    def test_buy_generic_exception_still_uses_long_cooldown(self, _mock_sleep):
        """Non-PolyApiException exceptions should still use the longer
        BUY_EXCEPTION_COOLDOWN (order may be in-flight on-chain)."""
        import config
        trader = _FakeTrader()
        state = _make_state_no_position()

        trader.client.create_market_order.return_value = "mock_order"
        trader.client.post_order.side_effect = RuntimeError("Unknown internal error")
        # pre-balance=0, verification returns 0
        trader._token_balances = [0.0, 0.0, 0.0]

        before = time.time()
        result = trader.buy(state, "tok_up_123", "Up", worst_price=0.65)

        self.assertFalse(result)
        expected_min = before + config.BUY_EXCEPTION_COOLDOWN
        self.assertGreaterEqual(
            state.buy_blocked_until, expected_min,
            "Generic exceptions should still use the longer BUY_EXCEPTION_COOLDOWN"
        )


# ── Sell tests ───────────────────────────────────────────────────────────────

@patch("trader.time.sleep")
class TestSellPolyApiException(unittest.TestCase):
    """Verify that sell() handles PolyApiException with proper verification."""

    def test_sell_network_error_still_verifies(self, _mock_sleep):
        """After PolyApiException during sell, verification should still run
        and detect if the sell actually filled on-chain."""
        trader = _FakeTrader()
        state = _make_state_with_position()

        trader.client.create_market_order.return_value = "mock_order"
        trader.client.post_order.side_effect = PolyApiException(
            error_msg="Request exception!"
        )

        # Pre-sell balance: 3.0, post-sell verification: 0.0 (filled!)
        balances = iter([3.0, 0.0])
        trader.get_token_balance = lambda _tid: next(balances)

        result = trader.sell(state, "SL")

        self.assertTrue(result, "sell() should detect fill despite PolyApiException")
        self.assertIsNone(state.position)

    def test_sell_network_error_increments_attempts(self, _mock_sleep):
        """PolyApiException during sell should increment sell_attempts
        via mark_sell_failed() for escalation tracking."""
        trader = _FakeTrader()
        state = _make_state_with_position()

        trader.client.create_market_order.return_value = "mock_order"
        trader.client.post_order.side_effect = PolyApiException(
            error_msg="Request exception!"
        )

        # Balance still present after sell attempt
        trader._token_balance = 3.0

        result = trader.sell(state, "SL")

        self.assertFalse(result)
        self.assertEqual(state.sell_attempts, 1, "sell_attempts should be incremented")

    def test_sell_http_error_verifies_and_retries(self, _mock_sleep):
        """PolyApiException with HTTP status during sell should verify
        and allow retry via mark_sell_failed()."""
        import httpx

        trader = _FakeTrader()
        state = _make_state_with_position()

        trader.client.create_market_order.return_value = "mock_order"
        mock_resp = MagicMock(spec=httpx.Response)
        mock_resp.status_code = 500
        mock_resp.json.return_value = {"error": "Internal Server Error"}
        trader.client.post_order.side_effect = PolyApiException(resp=mock_resp)

        # Balance still present
        trader._token_balance = 3.0

        result = trader.sell(state, "TP")

        self.assertFalse(result)
        self.assertEqual(state.sell_attempts, 1)
        self.assertIsNotNone(state.position, "Position should still exist")


if __name__ == "__main__":
    unittest.main()
