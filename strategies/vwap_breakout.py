"""
VWAP Breakout strategy.

BUY:  Price sustains above VWAP for 3 consecutive candles + RSI > 50
SELL: Price sustains below VWAP for 3 consecutive candles + RSI < 50
"""

from __future__ import annotations

import numpy as np
import pandas as pd

from core.strategy import BaseStrategy


class VWAPBreakoutStrategy(BaseStrategy):
    name = "VWAP Breakout"

    def compute_indicators(self) -> None:
        df = self.df

        # VWAP
        typical = (df["high"] + df["low"] + df["close"]) / 3
        cum_tp_vol = (typical * df["volume"]).cumsum()
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
        if len(df) < 16:
            return "HOLD"

        last3 = df.iloc[-3:]
        curr = df.iloc[-1]
        rsi = curr.get("rsi", 50)

        above = all(last3["close"] > last3["vwap"])
        below = all(last3["close"] < last3["vwap"])

        if above and rsi > 50:
            return "BUY"
        if below and rsi < 50:
            return "SELL"
        return "HOLD"
