"""
Opening Range Breakout (ORB) strategy.

Capture the high/low of the first 15-minute candle (9:15-9:30).
BUY:  Price breaks above ORB high with volume confirmation.
SELL: Price breaks below ORB low with volume confirmation.
SL at the opposite end of the ORB range.
"""

from __future__ import annotations

from datetime import time as dt_time

import pandas as pd

from config import settings
from core.strategy import BaseStrategy, Signal
from utils.logger import get_logger

log = get_logger(__name__)

# Don't trade ORB breakout within the opening range itself
_ORB_END_MIN = 15 + settings.ORB_CANDLE_MINUTES
_ORB_END_HOUR = 9 + _ORB_END_MIN // 60
_ORB_END_MIN = _ORB_END_MIN % 60
ORB_END_TIME = dt_time(_ORB_END_HOUR, _ORB_END_MIN)

# Minimum candle width to avoid noise breakouts (0.1% of price)
MIN_ORB_RANGE_PCT = 0.1


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

        # Identify the opening range candle(s)
        opening_candles = []
        for ts in df.index:
            t = ts.time() if hasattr(ts, "time") else None
            if t is None:
                continue
            if t < ORB_END_TIME:
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

        # Validate ORB range is meaningful (not too narrow)
        orb_range = self._orb_high - self._orb_low
        mid_price = (self._orb_high + self._orb_low) / 2
        if mid_price > 0 and (orb_range / mid_price * 100) < MIN_ORB_RANGE_PCT:
            return "HOLD"

        df = self.df
        if len(df) < 3:
            return "HOLD"

        curr = df.iloc[-1]
        prev = df.iloc[-2]

        # Don't signal during the opening range period
        curr_time = curr.name.time() if hasattr(curr.name, "time") else None
        if curr_time is not None and curr_time < ORB_END_TIME:
            return "HOLD"

        # Guard against NaN volume
        vol_avg = curr.get("vol_avg", 0)
        vol_ok = curr["volume"] > 1.2 * vol_avg if vol_avg > 0 else False

        # Breakout above ORB high
        if prev["close"] <= self._orb_high and curr["close"] > self._orb_high and vol_ok:
            return "BUY"

        # Breakdown below ORB low
        if prev["close"] >= self._orb_low and curr["close"] < self._orb_low and vol_ok:
            return "SELL"

        return "HOLD"

    def compute_signal_strength(self) -> Signal:
        """Score based on breakout distance and volume strength."""
        if self.df.empty or len(self.df) < 3:
            return Signal("HOLD", 0.0, self.name)

        self.compute_indicators()
        direction = self.generate_signal()
        if direction == "HOLD":
            return Signal("HOLD", 0.0, self.name)

        curr = self.df.iloc[-1]
        score = 0.4  # Base: clean breakout through ORB level

        # Distance past the ORB boundary — further = stronger
        if direction == "BUY" and self._orb_high:
            overshoot = (curr["close"] - self._orb_high) / self._orb_high * 100
            score += min(overshoot * 0.2, 0.3)
        elif direction == "SELL" and self._orb_low:
            overshoot = (self._orb_low - curr["close"]) / self._orb_low * 100
            score += min(overshoot * 0.2, 0.3)

        # Volume strength
        vol_avg = curr.get("vol_avg", 0)
        if vol_avg > 0:
            vol_ratio = curr["volume"] / vol_avg
            score += min((vol_ratio - 1.0) * 0.15, 0.3)

        return Signal(direction, min(score, 1.0), self.name)

    @property
    def orb_high(self) -> float | None:
        return self._orb_high

    @property
    def orb_low(self) -> float | None:
        return self._orb_low
