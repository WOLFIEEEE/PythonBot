"""
Download historical OHLCV candles from Kite Connect and store as CSV.
"""

from __future__ import annotations

import os
import time
from datetime import date, datetime, timedelta

import pandas as pd
from kiteconnect import KiteConnect

from utils.helpers import retry
from utils.logger import get_logger

log = get_logger(__name__)

DATA_DIR = os.path.join(os.path.dirname(__file__), "data")


@retry(max_retries=3, exceptions=(Exception,))
def _fetch_chunk(
    kite: KiteConnect,
    token: int,
    from_dt: datetime,
    to_dt: datetime,
    interval: str,
) -> list[dict]:
    """Fetch a single chunk of historical data (with retry + rate limiting)."""
    data = kite.historical_data(token, from_dt, to_dt, interval)
    time.sleep(1)  # Kite rate limit: ~1 req/sec for historical
    return data


def download_historical(
    kite: KiteConnect,
    instrument_token: int,
    symbol: str,
    from_date: date,
    to_date: date,
    interval: str = "5minute",
    save_csv: bool = True,
) -> pd.DataFrame:
    """
    Download historical candles in 60-day chunks and merge.

    Parameters
    ----------
    interval : '5minute', 'minute', '15minute', 'day', etc.
    """
    all_rows: list[dict] = []
    chunk_days = 55  # stay within Kite's 60-day limit for intraday

    current_from = datetime.combine(from_date, datetime.min.time())
    final_to = datetime.combine(to_date, datetime.min.time())

    while current_from < final_to:
        current_to = min(current_from + timedelta(days=chunk_days), final_to)
        log.info(
            "Fetching %s [%s → %s] interval=%s",
            symbol,
            current_from.date(),
            current_to.date(),
            interval,
        )
        rows = _fetch_chunk(kite, instrument_token, current_from, current_to, interval)
        all_rows.extend(rows)
        current_from = current_to + timedelta(days=1)

    if not all_rows:
        log.warning("No data returned for %s.", symbol)
        return pd.DataFrame()

    df = pd.DataFrame(all_rows)
    df.rename(
        columns={"date": "timestamp"},
        inplace=True,
    )
    df.set_index("timestamp", inplace=True)

    if save_csv:
        os.makedirs(DATA_DIR, exist_ok=True)
        path = os.path.join(DATA_DIR, f"{symbol}_{interval}_{from_date}_{to_date}.csv")
        df.to_csv(path)
        log.info("Saved %d candles to %s", len(df), path)

    return df
