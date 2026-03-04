"""
VWAP Breakout strategy.

BUY:  Price sustains above VWAP for 3 consecutive candles + RSI > 50
SELL: Price sustains below VWAP for 3 consecutive candles + RSI < 50
"""

from __future__ import annotations

import numpy as np
import pandas as pd

from core.strategy import BaseStrategy, Signal
from utils.logger import get_logger

log = get_logger(__name__)

# Need at least 16 candles for RSI warmup + 3-candle confirmation
MIN_CANDLES = 18


class VWAPBreakoutStrategy(BaseStrategy):
    name = "VWAP Breakout"

    def compute_indicators(self) -> None:
        df = self.df

        # VWAP (intraday cumulative, reset at market open daily)
        typical = (df["high"] + df["low"] + df["close"]) / 3
        tp_vol = typical * df["volume"]
        # Group by date to reset VWAP daily
        if hasattr(df.index, "date"):
            groups = df.index.date
            cum_tp_vol = tp_vol.groupby(groups).cumsum()
            cum_vol = df["volume"].groupby(groups).cumsum().replace(0, np.nan)
        else:
            cum_tp_vol = tp_vol.cumsum()
            cum_vol = df["volume"].cumsum().replace(0, np.nan)
        df["vwap"] = cum_tp_vol / cum_vol

        # RSI(14)
        delta = df["close"].diff()
        gain = delta.clip(lower=0)
        loss = -delta.clip(upper=0)
        avg_gain = gain.ewm(alpha=1 / 14, min_periods=14, adjust=False).mean()
        avg_loss = loss.ewm(alpha=1 / 14, min_periods=14, adjust=False).mean()
        rs = avg_gain / avg_loss.replace(0, np.nan)
        df["rsi"] = 100 - (100 / (1 + rs))

    def generate_signal(self) -> str:
        df = self.df
        if len(df) < MIN_CANDLES:
            return "HOLD"

        last3 = df.iloc[-3:]
        curr = df.iloc[-1]
        rsi = curr.get("rsi", 50)

        # Guard against NaN
        if pd.isna(rsi):
            return "HOLD"
        if last3["vwap"].isna().any() or last3["close"].isna().any():
            return "HOLD"

        above = all(last3["close"] > last3["vwap"])
        below = all(last3["close"] < last3["vwap"])

        if above and rsi > 50:
            return "BUY"
        if below and rsi < 50:
            return "SELL"
        return "HOLD"

    def compute_signal_strength(self) -> Signal:
        """Score based on VWAP distance and RSI strength."""
        if self.df.empty or len(self.df) < MIN_CANDLES:
            return Signal("HOLD", 0.0, self.name)

        self.compute_indicators()
        direction = self.generate_signal()
        if direction == "HOLD":
            return Signal("HOLD", 0.0, self.name)

        curr = self.df.iloc[-1]
        last3 = self.df.iloc[-3:]
        score = 0.4  # Base: 3-candle sustained breakout

        # Distance from VWAP — further = stronger conviction
        vwap = curr.get("vwap", 0)
        if vwap > 0:
            pct_dist = abs(curr["close"] - vwap) / vwap * 100
            score += min(pct_dist * 0.15, 0.3)

        # RSI strength
        rsi = curr.get("rsi", 50)
        if direction == "BUY" and rsi > 55:
            score += min((rsi - 50) / 30, 0.2)
        elif direction == "SELL" and rsi < 45:
            score += min((50 - rsi) / 30, 0.2)

        # Consistency of close vs VWAP across candles
        if direction == "BUY":
            margin = (last3["close"] - last3["vwap"]).mean()
        else:
            margin = (last3["vwap"] - last3["close"]).mean()
        if vwap > 0 and margin > 0:
            score += min(margin / vwap * 100 * 0.1, 0.1)

        return Signal(direction, min(score, 1.0), self.name)
