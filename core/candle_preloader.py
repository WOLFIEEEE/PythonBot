"""
Historical candle preloader for strategy warmup at startup.

The Problem:
  Strategies need 15-30 candles before generating signals. With 5-min candles,
  that's 75-150 minutes of MISSED trading after bot starts. The best intraday
  window (9:15-11:30) is completely lost.

The Solution:
  At startup, fetch historical 5-min candles from Kite's historical data API
  and pre-populate the CandleAggregator. This way strategies can generate
  signals from the very first live candle.

Kite API Constraints:
  - Historical API: 3 requests/second rate limit
  - 5-minute candles: max 100 days per request
  - We only need today's candles (if market already open) + previous 2 days for warmup

Usage:
  from core.candle_preloader import preload_candles
  preload_candles(kite, data_feed, token_map)
"""

from __future__ import annotations

import time
from datetime import date, datetime, timedelta
from typing import TYPE_CHECKING

import pandas as pd

from config import settings
from utils.logger import get_logger

if TYPE_CHECKING:
    from kiteconnect import KiteConnect

    from core.data_feed import CandleAggregator, DataFeed

log = get_logger(__name__)

# Kite historical API rate limit: 3 requests/sec — use 0.35s sleep to be safe
_RATE_LIMIT_DELAY = 0.35

# How many days of historical candles to preload (provides enough warmup)
_PRELOAD_DAYS = 5

# Kite historical API interval string for 5-minute candles
_INTERVAL_MAP = {
    1: "minute",
    3: "3minute",
    5: "5minute",
    10: "10minute",
    15: "15minute",
    30: "30minute",
    60: "60minute",
}


def preload_candles(
    kite: KiteConnect,
    data_feed: DataFeed,
    token_map: dict[str, int],
    days: int = _PRELOAD_DAYS,
) -> dict[str, int]:
    """
    Fetch historical candles and inject into the DataFeed's CandleAggregator.

    Args:
        kite: Authenticated KiteConnect instance
        data_feed: DataFeed with CandleAggregator to populate
        token_map: {symbol: instrument_token}
        days: Number of trading days to preload

    Returns:
        {symbol: candle_count} for each symbol successfully preloaded
    """
    interval_min = settings.CANDLE_INTERVAL_MINUTES
    interval_str = _INTERVAL_MAP.get(interval_min)
    if not interval_str:
        log.warning(
            "No historical interval mapping for %d minutes — skipping preload.",
            interval_min,
        )
        return {}

    # Calculate date range: from N trading days ago to now
    to_dt = datetime.now()
    from_dt = to_dt - timedelta(days=days + 2)  # +2 for weekends/holidays

    log.info(
        "Preloading %d-min historical candles for %d instruments (%s to %s)...",
        interval_min, len(token_map), from_dt.date(), to_dt.date(),
    )

    result: dict[str, int] = {}
    failed: list[str] = []

    for symbol, token in token_map.items():
        try:
            candles = _fetch_historical(kite, token, from_dt, to_dt, interval_str)
            if not candles:
                log.warning("No historical candles for %s — will rely on live data.", symbol)
                failed.append(symbol)
                continue

            count = _inject_into_aggregator(data_feed.aggregator, symbol, candles)
            result[symbol] = count

            if count > 0:
                log.debug("Preloaded %d candles for %s", count, symbol)

        except Exception as exc:
            log.warning("Failed to preload %s: %s", symbol, exc)
            failed.append(symbol)

        # Rate limit: 3 requests/second max
        time.sleep(_RATE_LIMIT_DELAY)

    total = sum(result.values())
    log.info(
        "Preload complete: %d candles across %d instruments. "
        "Failed: %d (%s)",
        total, len(result),
        len(failed), ", ".join(failed) if failed else "none",
    )

    return result


def preload_15min_candles(
    kite: KiteConnect,
    token_map: dict[str, int],
    days: int = _PRELOAD_DAYS,
) -> dict[str, pd.DataFrame]:
    """
    Fetch 15-minute historical candles for the multi-timeframe filter.

    Returns {symbol: DataFrame} with 15-min OHLCV candles.
    """
    to_dt = datetime.now()
    from_dt = to_dt - timedelta(days=days + 2)

    log.info("Preloading 15-min candles for MTF filter (%d instruments)...", len(token_map))

    result: dict[str, pd.DataFrame] = {}

    for symbol, token in token_map.items():
        try:
            candles = _fetch_historical(kite, token, from_dt, to_dt, "15minute")
            if candles:
                df = pd.DataFrame(candles)
                df.rename(columns={"date": "timestamp"}, inplace=True)
                df.set_index("timestamp", inplace=True)
                result[symbol] = df
        except Exception as exc:
            log.warning("Failed to preload 15-min for %s: %s", symbol, exc)

        time.sleep(_RATE_LIMIT_DELAY)

    log.info("15-min preload complete: %d instruments loaded.", len(result))
    return result


def _fetch_historical(
    kite: KiteConnect,
    token: int,
    from_dt: datetime,
    to_dt: datetime,
    interval: str,
    max_retries: int = 2,
) -> list[dict]:
    """
    Fetch historical candles from Kite with retry logic.

    Returns list of candle dicts: [{date, open, high, low, close, volume}, ...]
    """
    for attempt in range(max_retries + 1):
        try:
            data = kite.historical_data(token, from_dt, to_dt, interval)
            return data
        except Exception as exc:
            if attempt < max_retries:
                wait = 1.0 * (attempt + 1)
                log.debug(
                    "Historical fetch retry %d for token %d: %s (waiting %.1fs)",
                    attempt + 1, token, exc, wait,
                )
                time.sleep(wait)
            else:
                raise
    return []


def _inject_into_aggregator(
    aggregator: CandleAggregator,
    symbol: str,
    candles: list[dict],
) -> int:
    """
    Inject historical candles directly into the CandleAggregator's internal store.

    This bypasses the tick-by-tick flow and directly populates the candle history.
    """
    if not candles:
        return 0

    rows = []
    for c in candles:
        ts = c.get("date")
        if ts is None:
            continue
        # Handle timezone-aware datetimes from Kite
        if hasattr(ts, "tzinfo") and ts.tzinfo is not None:
            ts = ts.replace(tzinfo=None)
        rows.append({
            "timestamp": ts,
            "open": float(c.get("open", 0)),
            "high": float(c.get("high", 0)),
            "low": float(c.get("low", 0)),
            "close": float(c.get("close", 0)),
            "volume": int(c.get("volume", 0)),
        })

    if not rows:
        return 0

    # Only keep last MAX_CANDLES_IN_MEMORY candles
    max_candles = aggregator.max_candles
    if len(rows) > max_candles:
        rows = rows[-max_candles:]

    # Inject into the aggregator's internal candle store
    aggregator._candles[symbol] = rows

    # Set the last valid price from the most recent candle
    if rows:
        aggregator._last_valid_price[symbol] = rows[-1]["close"]

    # Set previous day close for gap detection from the candle data
    # Find the last candle from a previous trading day
    today = date.today()
    for r in reversed(rows):
        candle_date = r["timestamp"].date() if hasattr(r["timestamp"], "date") else today
        if candle_date < today:
            aggregator._prev_day_close[symbol] = r["close"]
            break

    return len(rows)


def get_warmup_status(
    data_feed: DataFeed,
    token_map: dict[str, int],
    min_candles: int = 30,
) -> dict[str, dict]:
    """
    Check whether each instrument has enough candles for strategy warmup.

    Returns {symbol: {"count": N, "ready": bool, "min_required": M}}
    """
    result = {}
    for symbol in token_map:
        count = data_feed.aggregator.get_candle_count(symbol)
        result[symbol] = {
            "count": count,
            "ready": count >= min_candles,
            "min_required": min_candles,
        }
    return result
