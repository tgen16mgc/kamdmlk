import asyncio
import logging
import signal
import time
from datetime import datetime, timezone

import config
from logger_setup import setup_logging
from state import BotState
from trader import Trader
from strategy import MomentumStrategy
from market_finder import discover_market
from price_feed import run_price_feed
from market_feed import run_market_feed

logger = logging.getLogger("main")


async def strategy_loop(strategy: MomentumStrategy, state: BotState):
    """Run the strategy tick at a fixed interval."""
    while state.running:
        try:
            strategy.tick()
        except Exception as e:
            logger.error(f"Strategy tick error: {e}", exc_info=True)
        await asyncio.sleep(config.STRATEGY_LOOP_INTERVAL)


async def market_discovery_loop(state: BotState):
    """
    Continuously discover the active 5-min BTC market.
    When the current market expires or resolves, find the next one.
    """
    while state.running:
        try:
            remaining = state.time_remaining()
            has_market = state.current_condition_id is not None
            market_expired = remaining is not None and remaining <= 0
            market_resolved = state.market_resolved

            needs_new_market = (
                not has_market
                or market_expired
                or market_resolved
            )

            if needs_new_market:
                if has_market and (market_expired or market_resolved):
                    # Handle position if still open when market expires
                    if state.has_position() and not market_resolved:
                        logger.warning(
                            "Market expired with open position - "
                            "waiting for resolution..."
                        )
                        await asyncio.sleep(5)
                        continue

                    # Consume cooldown round
                    state.consume_cooldown()
                    logger.info(f"SESSION: {state.summary()}")

                found = discover_market(state)
                if not found:
                    logger.debug("No active 5-min BTC market found, retrying...")

        except Exception as e:
            logger.error(f"Market discovery error: {e}", exc_info=True)

        await asyncio.sleep(config.MARKET_POLL_INTERVAL)


async def daily_report_loop(state: BotState, trader: Trader):
    """Send a summary report to Telegram twice a day at 08:00 and 20:00 UTC."""
    from telegram_notify import notify_daily_report
    report_hours = {8, 20}
    last_reported_hour: int = -1

    while state.running:
        try:
            now_utc = datetime.now(timezone.utc)
            if now_utc.hour in report_hours and now_utc.hour != last_reported_hour:
                last_reported_hour = now_utc.hour
                balance = trader.get_usdc_balance()
                notify_daily_report(
                    wins=state.wins,
                    losses=state.losses,
                    session_pnl=state.session_pnl,
                    balance=balance,
                    starting_balance=state.starting_balance,
                    btc_price=state.btc_current,
                )
                logger.info(f"Daily report sent (UTC {now_utc.hour:02d}:00)")
            elif now_utc.hour not in report_hours:
                last_reported_hour = -1  # reset so next window fires
        except Exception as e:
            logger.debug(f"Daily report error: {e}")
        await asyncio.sleep(60)  # check every minute


async def price_polling_fallback(state: BotState, trader: Trader):
    """
    Fallback price polling via REST API when WebSocket doesn't provide
    best_bid_ask (e.g. if custom_feature_enabled isn't supported).
    Polls every 2 seconds.
    """
    while state.running:
        try:
            for direction, token_id in [("up", state.up_token_id), ("down", state.down_token_id)]:
                if not token_id:
                    continue
                try:
                    book = trader.client.get_order_book(token_id)
                    if book.bids:
                        bid = float(book.bids[-1].price)
                        if direction == "up":
                            state.up_best_bid = bid
                        else:
                            state.down_best_bid = bid
                    if book.asks:
                        ask = float(book.asks[-1].price)
                        if direction == "up":
                            state.up_best_ask = ask
                        else:
                            state.down_best_ask = ask
                except Exception:
                    pass

        except Exception as e:
            logger.debug(f"Price polling error: {e}")

        await asyncio.sleep(2)


async def main():
    setup_logging()
    logger.info("=" * 60)
    logger.info("BTC 5-Min Momentum Bot Starting")
    logger.info("=" * 60)
    logger.info(f"Signer: {config.SIGNER_ADDRESS}")
    logger.info(f"Funder: {config.FUNDER_ADDRESS}")
    logger.info(f"Bet size: ${config.BET_SIZE} | All-in: {config.ALL_IN}")
    logger.info(
        f"Entry window: {config.ENTRY_WINDOW_MAX}s - {config.ENTRY_WINDOW_MIN}s | "
        f"Price: ${config.ENTRY_PRICE_MIN}-${config.ENTRY_PRICE_MAX}"
    )
    logger.info(
        f"TP: ${config.TAKE_PROFIT} | SL: ${config.STOP_LOSS} | "
        f"Time stop: {config.TIME_STOP_SECONDS}s (hard: {config.HARD_TIME_STOP_SECONDS}s) | "
        f"Breakeven stop: {config.BREAKEVEN_TIME_STOP_SECONDS}s"
    )
    logger.info(f"BTC momentum min: ${config.BTC_MOMENTUM_MIN}")
    logger.info(f"Session stop-loss: {config.SESSION_STOP_LOSS_PCT * 100:.0f}% of starting balance")
    logger.info("=" * 60)

    state = BotState()
    trader = Trader()

    # Initialize CLOB client (retry on transient network errors)
    for attempt in range(1, 6):
        try:
            trader.initialize()
            break
        except Exception as e:
            logger.warning(f"Trader init attempt {attempt}/5 failed: {e}")
            if attempt == 5:
                logger.critical("Could not initialize trader after 5 attempts. Exiting.")
                return
            await asyncio.sleep(5)

    balance = trader.get_usdc_balance()
    state.starting_balance = balance
    logger.info(f"USDC.e balance: ${balance:.2f}")

    strategy = MomentumStrategy(state, trader)

    # Graceful shutdown
    loop = asyncio.get_running_loop()

    def shutdown_handler():
        logger.info("Shutdown signal received...")
        state.running = False

    for sig in (signal.SIGINT, signal.SIGTERM):
        loop.add_signal_handler(sig, shutdown_handler)

    # Launch all concurrent tasks
    tasks = [
        asyncio.create_task(run_price_feed(state), name="price_feed"),
        asyncio.create_task(run_market_feed(state), name="market_feed"),
        asyncio.create_task(market_discovery_loop(state), name="market_discovery"),
        asyncio.create_task(strategy_loop(strategy, state), name="strategy"),
        asyncio.create_task(trader.heartbeat_loop(state), name="heartbeat"),
        asyncio.create_task(price_polling_fallback(state, trader), name="price_poll"),
        asyncio.create_task(daily_report_loop(state, trader), name="daily_report"),
    ]

    logger.info("All systems running. Waiting for market...")

    try:
        await asyncio.gather(*tasks)
    except asyncio.CancelledError:
        pass
    finally:
        state.running = False
        # Cancel any open orders
        trader.cancel_all_orders()

        for task in tasks:
            task.cancel()
        await asyncio.gather(*tasks, return_exceptions=True)

        logger.info("=" * 60)
        logger.info("Bot stopped. Final session stats:")
        logger.info(state.summary())
        logger.info("=" * 60)


if __name__ == "__main__":
    asyncio.run(main())
