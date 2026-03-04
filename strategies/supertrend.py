"""
Supertrend indicator strategy.

BUY:  Close crosses above Supertrend line.
SELL: Close crosses below Supertrend line.
Uses ATR-based stop-loss.
"""

from __future__ import annotations

import numpy as np
import pandas as pd

from config import settings
from core.strategy import BaseStrategy, Signal
from utils.logger import get_logger

log = get_logger(__name__)

MIN_CANDLES = settings.SUPERTREND_PERIOD + 5


def _compute_supertrend(
    df: pd.DataFrame,
    period: int = settings.SUPERTREND_PERIOD,
    multiplier: float = settings.SUPERTREND_MULTIPLIER,
) -> pd.DataFrame:
    """Add 'supertrend' and 'st_direction' columns to df."""
    hl2 = (df["high"] + df["low"]) / 2

    # ATR
    tr1 = df["high"] - df["low"]
    tr2 = (df["high"] - df["close"].shift()).abs()
    tr3 = (df["low"] - df["close"].shift()).abs()
    tr = pd.concat([tr1, tr2, tr3], axis=1).max(axis=1)
    atr = tr.rolling(period).mean()

    upper_band = hl2 + multiplier * atr
    lower_band = hl2 - multiplier * atr

    supertrend = pd.Series(np.nan, index=df.index)
    direction = pd.Series(1, index=df.index)  # 1 = up, -1 = down

    for i in range(period, len(df)):
        if df["close"].iloc[i] > upper_band.iloc[i - 1]:
            direction.iloc[i] = 1
        elif df["close"].iloc[i] < lower_band.iloc[i - 1]:
            direction.iloc[i] = -1
        else:
            direction.iloc[i] = direction.iloc[i - 1]

            if direction.iloc[i] == 1 and lower_band.iloc[i] < lower_band.iloc[i - 1]:
                lower_band.iloc[i] = lower_band.iloc[i - 1]
            if direction.iloc[i] == -1 and upper_band.iloc[i] > upper_band.iloc[i - 1]:
                upper_band.iloc[i] = upper_band.iloc[i - 1]

        supertrend.iloc[i] = lower_band.iloc[i] if direction.iloc[i] == 1 else upper_band.iloc[i]

    df["supertrend"] = supertrend
    df["st_direction"] = direction
    df["atr"] = atr
    return df


class SupertrendStrategy(BaseStrategy):
    name = "Supertrend"

    def compute_indicators(self) -> None:
        _compute_supertrend(self.df)

    def generate_signal(self) -> str:
        df = self.df
        if len(df) < MIN_CANDLES:
            return "HOLD"

        prev = df.iloc[-2]
        curr = df.iloc[-1]

        # Guard against NaN
        if pd.isna(curr.get("supertrend")) or pd.isna(prev.get("supertrend")):
            return "HOLD"
        if pd.isna(curr.get("st_direction")) or pd.isna(prev.get("st_direction")):
            return "HOLD"

        prev_dir = int(prev["st_direction"])
        curr_dir = int(curr["st_direction"])

        # Crossover: direction changed from -1 to 1 -> BUY
        if prev_dir == -1 and curr_dir == 1:
            return "BUY"
        # Crossover: direction changed from 1 to -1 -> SELL
        if prev_dir == 1 and curr_dir == -1:
            return "SELL"

        return "HOLD"

    def compute_signal_strength(self) -> Signal:
        """Score based on distance from supertrend line and ATR context."""
        if self.df.empty or len(self.df) < MIN_CANDLES:
            return Signal("HOLD", 0.0, self.name)

        self.compute_indicators()
        direction = self.generate_signal()
        if direction == "HOLD":
            return Signal("HOLD", 0.0, self.name)

        curr = self.df.iloc[-1]
        score = 0.5  # Base: direction crossover happened

        # Distance from supertrend line — bigger gap = stronger signal
        st_val = curr.get("supertrend", 0)
        atr = curr.get("atr", 0)
        if atr > 0 and st_val > 0:
            dist = abs(curr["close"] - st_val) / atr
            score += min(dist * 0.15, 0.3)

        # Volume boost
        vol_avg = self.df["volume"].rolling(20).mean().iloc[-1]
        if vol_avg > 0 and curr["volume"] > vol_avg:
            score += 0.2

        return Signal(direction, min(score, 1.0), self.name)
