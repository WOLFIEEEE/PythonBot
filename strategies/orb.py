"""
Opening Range Breakout (ORB) strategy.

Capture the high/low of the first 15-minute candle (9:15–9:30).
BUY:  Price breaks above ORB high with volume confirmation.
SELL: Price breaks below ORB low with volume confirmation.
SL at the opposite end of the ORB range.
"""

from __future__ import annotations

import pandas as pd

from config import settings
from core.strategy import BaseStrategy


class ORBStrategy(BaseStrategy):
    name = "ORB"

    def __init__(self, instrument: str, candle_df: pd.DataFrame):
        super().__init__(instrument, candle_df)
        self._orb_high: float | None = None
        self._orb_low: float | None = None

    def compute_indicators(self) -> None:
        df = self.df
        if df.empty:
            return

        # Identify the opening range candle(s) — first ORB_CANDLE_MINUTES
        # We assume candle timestamps are timezone-naive IST
        opening_candles = []
        for ts in df.index:
            t = ts.time() if hasattr(ts, "time") else None
            if t is None:
                continue
            from datetime import time as dt_time
            orb_end_hour = 9
            orb_end_min = 15 + settings.ORB_CANDLE_MINUTES
            if orb_end_min >= 60:
                orb_end_hour += orb_end_min // 60
                orb_end_min = orb_end_min % 60
            if t < dt_time(orb_end_hour, orb_end_min):
                opening_candles.append(ts)

        if not opening_candles:
            return

        orb_df = df.loc[opening_candles]
        self._orb_high = orb_df["high"].max()
        self._orb_low = orb_df["low"].min()

        # Average volume for confirmation
        df["vol_avg"] = df["volume"].rolling(20).mean()

    def generate_signal(self) -> str:
        if self._orb_high is None or self._orb_low is None:
            return "HOLD"

        df = self.df
        if len(df) < 3:
            return "HOLD"

        curr = df.iloc[-1]
        prev = df.iloc[-2]

        vol_ok = curr["volume"] > 1.2 * curr.get("vol_avg", 0) if curr.get("vol_avg", 0) > 0 else True

        # Breakout above ORB high
        if prev["close"] <= self._orb_high and curr["close"] > self._orb_high and vol_ok:
            return "BUY"

        # Breakdown below ORB low
        if prev["close"] >= self._orb_low and curr["close"] < self._orb_low and vol_ok:
            return "SELL"

        return "HOLD"

    @property
    def orb_high(self) -> float | None:
        return self._orb_high

    @property
    def orb_low(self) -> float | None:
        return self._orb_low
