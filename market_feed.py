import asyncio
import json
import logging

import websockets

import config
from state import BotState

logger = logging.getLogger("market_feed")


async def run_market_feed(state: BotState):
    """
    Connects to the Polymarket Market WebSocket to stream orderbook updates,
    trade prices, and market_resolved events for the active market's tokens.
    Automatically resubscribes when the market changes.
    """
    while state.running:
        if state.up_token_id is None or state.down_token_id is None:
            await asyncio.sleep(1)
            continue

        current_up = state.up_token_id
        current_down = state.down_token_id

        try:
            proxy_kwargs = {"proxy": config.PROXY_URL} if config.PROXY_URL else {}
            async with websockets.connect(
                config.MARKET_WS,
                ping_interval=None,
                **proxy_kwargs,
                close_timeout=5,
            ) as ws:
                sub_msg = json.dumps({
                    "type": "market",
                    "assets_ids": [current_up, current_down],
                    "custom_feature_enabled": True,
                })
                await ws.send(sub_msg)
                logger.info(
                    f"Market WS subscribed: up={current_up[:16]}... down={current_down[:16]}..."
                )

                ping_task = asyncio.create_task(_ping_loop(ws, state))

                try:
                    async for raw in ws:
                        if not state.running:
                            break

                        if state.up_token_id != current_up or state.down_token_id != current_down:
                            logger.info("Market changed, reconnecting WebSocket...")
                            break

                        if raw == "PONG":
                            continue

                        try:
                            msg = json.loads(raw)
                        except json.JSONDecodeError:
                            continue

                        # The WS can send a single dict OR a list of dicts (batch)
                        if isinstance(msg, list):
                            for item in msg:
                                if isinstance(item, dict):
                                    _handle_message(state, item, current_up, current_down)
                        elif isinstance(msg, dict):
                            _handle_message(state, msg, current_up, current_down)
                finally:
                    ping_task.cancel()
                    try:
                        await ping_task
                    except asyncio.CancelledError:
                        pass

        except websockets.ConnectionClosed as e:
            logger.warning(f"Market WS closed: {e}. Reconnecting in 2s...")
        except Exception as e:
            logger.error(f"Market WS error: {e}. Reconnecting in 3s...")

        if state.running:
            await asyncio.sleep(2)

    logger.info("Market feed stopped")


async def _ping_loop(ws, state: BotState):
    try:
        while state.running:
            await asyncio.sleep(config.WS_PING_INTERVAL)
            try:
                await ws.send("PING")
            except Exception:
                break
    except asyncio.CancelledError:
        pass


def _handle_message(
    state: BotState, msg: dict, up_token: str, down_token: str
):
    event_type = msg.get("event_type")

    if event_type == "best_bid_ask":
        _handle_best_bid_ask(state, msg, up_token, down_token)

    elif event_type == "last_trade_price":
        _handle_last_trade(state, msg, up_token, down_token)

    elif event_type == "book":
        _handle_book_snapshot(state, msg, up_token, down_token)

    elif event_type == "price_change":
        pass

    elif event_type == "market_resolved":
        _handle_resolved(state, msg, up_token, down_token)


def _handle_best_bid_ask(state: BotState, msg: dict, up_token: str, down_token: str):
    asset_id = msg.get("asset_id", "")
    try:
        bid = msg.get("best_bid")
        ask = msg.get("best_ask")
        if asset_id == up_token:
            if bid is not None:
                state.up_best_bid = float(bid)
            if ask is not None:
                state.up_best_ask = float(ask)
        elif asset_id == down_token:
            if bid is not None:
                state.down_best_bid = float(bid)
            if ask is not None:
                state.down_best_ask = float(ask)
    except (ValueError, TypeError):
        pass


def _handle_last_trade(state: BotState, msg: dict, up_token: str, down_token: str):
    asset_id = msg.get("asset_id", "")
    try:
        price = msg.get("price")
        if price is not None:
            if asset_id == up_token:
                state.up_last_trade = float(price)
            elif asset_id == down_token:
                state.down_last_trade = float(price)
    except (ValueError, TypeError):
        pass


def _handle_book_snapshot(state: BotState, msg: dict, up_token: str, down_token: str):
    """
    Extract best bid/ask from a full book snapshot.
    Bids are sorted ASCENDING (lowest first) — best bid is LAST.
    Asks are sorted DESCENDING (highest first) — best ask is LAST.
    """
    asset_id = msg.get("asset_id", "")
    if asset_id not in (up_token, down_token):
        return
    try:
        bids = msg.get("bids", [])
        asks = msg.get("asks", [])
        bid_price = None
        ask_price = None
        if bids:
            best = bids[-1] if isinstance(bids[-1], dict) else {}
            p = float(best.get("price", 0))
            if p > 0:
                bid_price = p
        if asks:
            best = asks[-1] if isinstance(asks[-1], dict) else {}
            p = float(best.get("price", 0))
            if p > 0:
                ask_price = p
        if asset_id == up_token:
            if bid_price is not None:
                state.up_best_bid = bid_price
            if ask_price is not None:
                state.up_best_ask = ask_price
        else:
            if bid_price is not None:
                state.down_best_bid = bid_price
            if ask_price is not None:
                state.down_best_ask = ask_price
    except (ValueError, TypeError, IndexError):
        pass


def _handle_resolved(
    state: BotState, msg: dict, up_token: str, down_token: str
):
    winning_id = msg.get("winning_asset_id", "")
    winning_outcome = msg.get("winning_outcome", "")
    state.market_resolved = True
    state.winning_token_id = winning_id

    direction = "Up" if winning_id == up_token else "Down"
    logger.info(
        f"MARKET RESOLVED: winner={direction} ({winning_outcome}) | "
        f"token={winning_id[:16]}..."
    )
