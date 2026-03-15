import asyncio
import json
import logging
import time

import websockets

import config
from state import BotState

logger = logging.getLogger("price_feed")

_last_btc_log = 0.0


async def run_price_feed(state: BotState):
    """
    Connects to the Polymarket RTDS WebSocket and streams live BTC/USD prices
    from Binance. Updates state.btc_current and state.btc_prices on every tick.
    """
    while state.running:
        try:
            proxy_kwargs = {"proxy": config.PROXY_URL} if config.PROXY_URL else {}
            async with websockets.connect(
                config.RTDS_WS,
                ping_interval=None,
                close_timeout=5,
                **proxy_kwargs,
            ) as ws:
                sub_msg = json.dumps({
                    "action": "subscribe",
                    "subscriptions": [{
                        "topic": "crypto_prices",
                        "type": "update",
                    }],
                })
                await ws.send(sub_msg)
                logger.info("RTDS WebSocket connected - streaming BTC/USD prices")

                ping_task = asyncio.create_task(_rtds_ping(ws, state))
                try:
                    async for raw in ws:
                        if not state.running:
                            break

                        if not raw or not raw.strip():
                            continue

                        try:
                            msg = json.loads(raw)
                        except json.JSONDecodeError:
                            continue

                        if msg.get("type") == "update" and msg.get("topic") == "crypto_prices":
                            payload = msg.get("payload", {})
                            symbol = payload.get("symbol", "")
                            if "btc" in symbol.lower():
                                price = payload.get("value")
                                if price is not None:
                                    global _last_btc_log
                                    state.record_btc_price(float(price))

                                    if state.btc_candle_open is None:
                                        state.btc_candle_open = float(price)
                                        logger.info(f"BTC candle open set: ${price:,.2f}")

                                    now = time.time()
                                    if now - _last_btc_log >= 2.0:
                                        _last_btc_log = now
                                        mom = state.btc_momentum()
                                        vel = state.btc_velocity(config.MOMENTUM_VELOCITY_WINDOW)
                                        mom_str = f"mom=${mom:+.2f}" if mom is not None else "mom=N/A"
                                        vel_str = f"vel={vel:+.4f}/s" if vel is not None else "vel=N/A"
                                        logger.info(f"BTC=${float(price):,.2f} | {mom_str} | {vel_str}")
                finally:
                    ping_task.cancel()
                    try:
                        await ping_task
                    except asyncio.CancelledError:
                        pass

        except websockets.ConnectionClosed as e:
            logger.warning(f"RTDS connection closed: {e}. Reconnecting in 2s...")
        except Exception as e:
            logger.error(f"RTDS error: {e}. Reconnecting in 3s...")

        if state.running:
            await asyncio.sleep(3)

    logger.info("Price feed stopped")


async def _rtds_ping(ws, state: BotState):
    try:
        while state.running:
            await asyncio.sleep(5)
            await ws.send("PING")
    except asyncio.CancelledError:
        pass
    except Exception:
        pass
