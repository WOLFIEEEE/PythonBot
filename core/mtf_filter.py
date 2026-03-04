"""
Multi-Timeframe (MTF) confirmation filter.

Aggregates 5-min candles into 15-min candles on-the-fly and checks
that the higher timeframe trend aligns with the 5-min entry signal.

Rule: Only take BUY signals if 15-min trend is UP (and vice versa).
This prevents entering against the macro structure and significantly
reduces whipsaw losses during counter-trend moves.
"""

from __future__ import annotations

import pandas as pd
import numpy as np

from utils.logger import get_logger

log = get_logger(__name__)


def aggregate_to_higher_tf(df: pd.DataFrame, factor: int = 3) -> pd.DataFrame:
    """
    Aggregate lower-timeframe candles into higher timeframe.
    Default: 3x (5-min → 15-min).

    Returns a DataFrame with OHLCV columns indexed by the period start time.
    """
    if df.empty or len(df) < factor:
        return pd.DataFrame()

    # Group every `factor` candles together
    n = len(df)
    # Trim to a multiple of factor from the end (keep latest data)
    trim = n % factor
    if trim > 0:
        trimmed = df.iloc[trim:]
    else:
        trimmed = df

    rows = []
    for i in range(0, len(trimmed), factor):
        chunk = trimmed.iloc[i:i + factor]
        if len(chunk) < factor:
            break
        rows.append({
            "timestamp": chunk.index[0],
            "open": chunk["open"].iloc[0],
            "high": chunk["high"].max(),
            "low": chunk["low"].min(),
            "close": chunk["close"].iloc[-1],
            "volume": chunk["volume"].sum(),
        })

    if not rows:
        return pd.DataFrame()

    htf = pd.DataFrame(rows)
    htf.set_index("timestamp", inplace=True)
    return htf


def get_htf_trend(df_5min: pd.DataFrame, ema_period: int = 9) -> str:
    """
    Determine the higher-timeframe trend direction.

    Aggregates 5-min to 15-min, then checks:
      - EMA(9) slope on 15-min
      - Last 2 candle closes relative to EMA

    Returns: "UP", "DOWN", or "NEUTRAL"
    """
    htf = aggregate_to_higher_tf(df_5min, factor=3)
    if len(htf) < ema_period + 2:
        return "NEUTRAL"

    htf["ema"] = htf["close"].ewm(span=ema_period, adjust=False).mean()

    curr = htf.iloc[-1]
    prev = htf.iloc[-2]

    if pd.isna(curr["ema"]) or pd.isna(prev["ema"]):
        return "NEUTRAL"

    ema_rising = curr["ema"] > prev["ema"]
    ema_falling = curr["ema"] < prev["ema"]
    above_ema = curr["close"] > curr["ema"] and prev["close"] > prev["ema"]
    below_ema = curr["close"] < curr["ema"] and prev["close"] < prev["ema"]

    if ema_rising and above_ema:
        return "UP"
    if ema_falling and below_ema:
        return "DOWN"
    return "NEUTRAL"


def mtf_confirms_signal(df_5min: pd.DataFrame, signal: str) -> bool:
    """
    Check if the higher-timeframe trend confirms the 5-min signal.

    BUY signal requires 15-min trend UP or NEUTRAL.
    SELL signal requires 15-min trend DOWN or NEUTRAL.
    HOLD always passes.

    Returns True if confirmed (or if insufficient data for HTF).
    """
    if signal == "HOLD":
        return True

    htf_trend = get_htf_trend(df_5min)

    if signal == "BUY":
        confirmed = htf_trend in ("UP", "NEUTRAL")
        if not confirmed:
            log.debug("MTF filter: BUY rejected — 15-min trend is %s", htf_trend)
        return confirmed

    if signal == "SELL":
        confirmed = htf_trend in ("DOWN", "NEUTRAL")
        if not confirmed:
            log.debug("MTF filter: SELL rejected — 15-min trend is %s", htf_trend)
        return confirmed

    return True
