import json
import logging
import time
from datetime import datetime, timezone

import requests

import config
from state import BotState

logger = logging.getLogger("market_finder")

# BTC 5-min markets use slug pattern: btc-updown-5m-{end_unix_timestamp}
# End timestamps are always 5-minute boundaries (multiples of 300 seconds).
SERIES_SLUG = "btc-up-or-down-5m"
SLUG_PREFIX = "btc-updown-5m-"
INTERVAL = 300  # 5 minutes in seconds


def _parse_iso(iso_str: str) -> float:
    if iso_str.endswith("Z"):
        iso_str = iso_str[:-1] + "+00:00"
    dt = datetime.fromisoformat(iso_str)
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return dt.timestamp()


def _next_5min_timestamps() -> list[int]:
    """Return the next several 5-min boundary timestamps from now."""
    now = int(time.time())
    current_boundary = (now // INTERVAL) * INTERVAL
    return [current_boundary + i * INTERVAL for i in range(0, 8)]


def _proxies() -> dict | None:
    if config.PROXY_URL:
        return {"http": config.PROXY_URL, "https": config.PROXY_URL}
    return None


def fetch_market_by_slug(slug: str) -> dict | None:
    """Fetch a single event by its slug from the Gamma API."""
    try:
        resp = requests.get(
            f"{config.GAMMA_HOST}/events",
            params={"slug": slug},
            timeout=8,
            proxies=_proxies(),
        )
        resp.raise_for_status()
        data = resp.json()
        if data and isinstance(data, list) and len(data) > 0:
            return data[0]
    except Exception as e:
        logger.debug(f"Slug fetch failed for {slug}: {e}")
    return None


def find_active_5min_btc_market() -> dict | None:
    """
    Finds the currently active 5-minute BTC up/down market using the
    deterministic slug pattern: btc-updown-5m-{end_unix_timestamp}.

    Tries the current 5-min window first, then the next few upcoming ones.
    """
    now = time.time()
    for ts in _next_5min_timestamps():
        slug = f"{SLUG_PREFIX}{ts}"
        event = fetch_market_by_slug(slug)
        if event is None:
            continue

        # Skip closed or expired
        end_iso = event.get("endDate", "")
        if not end_iso:
            continue
        try:
            end_ts = _parse_iso(end_iso)
        except Exception:
            continue

        remaining = end_ts - now
        if remaining <= 0:
            continue  # already expired

        if remaining > INTERVAL + 30:
            continue  # market too far in the future, wait for the current window

        if event.get("closed") or not event.get("active"):
            continue

        logger.debug(f"Found BTC 5m: slug={slug} | {remaining:.0f}s remaining")
        return event

    return None


def update_state_with_market(state: BotState, event: dict) -> bool:
    """
    Populate the bot state with market data from the event dict.
    Returns True if a new market was successfully loaded.
    """
    markets = event.get("markets", [])
    if not markets:
        return False

    market = markets[0]
    condition_id = market.get("conditionId") or market.get("condition_id")
    if not condition_id:
        return False

    if condition_id == state.current_condition_id:
        return False  # same market, nothing to update

    # Parse token IDs (clobTokenIds can be a JSON string or list)
    raw_tokens = market.get("clobTokenIds", "[]")
    if isinstance(raw_tokens, str):
        tokens = json.loads(raw_tokens)
    else:
        tokens = raw_tokens

    if len(tokens) < 2:
        logger.warning(f"Market {condition_id[:12]}... has < 2 tokens, skipping")
        return False

    # Parse outcomes to assign Up/Down correctly
    raw_outcomes = market.get("outcomes", '["Up","Down"]')
    if isinstance(raw_outcomes, str):
        outcomes = json.loads(raw_outcomes)
    else:
        outcomes = raw_outcomes

    up_idx, down_idx = 0, 1
    for i, outcome in enumerate(outcomes):
        o = str(outcome).lower()
        if o in ("up", "yes", "higher"):
            up_idx = i
        elif o in ("down", "no", "lower"):
            down_idx = i

    end_iso = market.get("endDate") or event.get("endDate", "")
    try:
        end_ts = _parse_iso(end_iso)
    except Exception:
        logger.error(f"Cannot parse end date: {end_iso}")
        return False

    state.reset_for_new_market()
    state.current_condition_id = condition_id
    state.current_market_id = market.get("id")
    state.up_token_id = tokens[up_idx]
    state.down_token_id = tokens[down_idx]
    state.market_end_time = end_ts
    state.market_neg_risk = event.get("negRisk") or market.get("negRisk") or False
    state.market_tick_size = str(
        market.get("orderPriceMinTickSize")
        or market.get("minimumTickSize")
        or market.get("minimum_tick_size")
        or "0.01"
    )

    # Set candle open if we already have a BTC price
    if state.btc_current is not None:
        state.btc_candle_open = state.btc_current

    remaining = end_ts - time.time()
    question = market.get("question") or event.get("title", "N/A")
    logger.info(
        f"NEW MARKET LOADED: {question} | "
        f"{remaining:.0f}s remaining | "
        f"tick={state.market_tick_size} | "
        f"up={state.up_token_id[:14]}... | "
        f"down={state.down_token_id[:14]}..."
    )
    return True


def discover_market(state: BotState) -> bool:
    """Main discovery entry point. Returns True if a new market was loaded."""
    event = find_active_5min_btc_market()
    if event is None:
        logger.debug("No active BTC 5m market found via slug lookup")
        return False
    return update_state_with_market(state, event)
