"""
EMA 9/21 Crossover strategy with VWAP, RSI, and volume confirmation.

BUY:  EMA(9) crosses above EMA(21) + close > VWAP + volume > 1.5x avg + RSI 40-70
SELL: EMA(9) crosses below EMA(21) + close < VWAP + RSI 30-60
"""

from __future__ import annotations

import numpy as np
import pandas as pd

from config import settings
from core.strategy import BaseStrategy, Signal
from utils.logger import get_logger

log = get_logger(__name__)

# Minimum candles required before generating any signal
MIN_CANDLES = max(settings.EMA_SLOW, 20) + 5


class EMACrossoverStrategy(BaseStrategy):
    name = "EMA Crossover"

    def compute_indicators(self) -> None:
        df = self.df
        df["ema_fast"] = df["close"].ewm(span=settings.EMA_FAST, adjust=False).mean()
        df["ema_slow"] = df["close"].ewm(span=settings.EMA_SLOW, adjust=False).mean()

        # VWAP (intraday cumulative, reset at market open 9:15)
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

        # Average volume (20-period)
        df["vol_avg"] = df["volume"].rolling(20).mean()

    def generate_signal(self) -> str:
        df = self.df
        if len(df) < MIN_CANDLES:
            return "HOLD"

        prev = df.iloc[-2]
        curr = df.iloc[-1]

        # Guard against NaN in critical indicators
        for col in ("ema_fast", "ema_slow", "rsi", "vwap"):
            if pd.isna(curr.get(col)) or pd.isna(prev.get(col, np.nan)):
                return "HOLD"

        # Crossover detection
        cross_up = prev["ema_fast"] <= prev["ema_slow"] and curr["ema_fast"] > curr["ema_slow"]
        cross_down = prev["ema_fast"] >= prev["ema_slow"] and curr["ema_fast"] < curr["ema_slow"]

        rsi = curr["rsi"]
        vol_avg = curr.get("vol_avg", 0)
        vol_ok = curr["volume"] > 1.5 * vol_avg if vol_avg > 0 else False
        above_vwap = curr["close"] > curr["vwap"]
        below_vwap = curr["close"] < curr["vwap"]

        if cross_up and above_vwap and vol_ok and 40 <= rsi <= 70:
            return "BUY"
        if cross_down and below_vwap and 30 <= rsi <= 60:
            return "SELL"
        return "HOLD"

    def compute_signal_strength(self) -> Signal:
        """Score 0-1 based on how many confirmations are present."""
        if self.df.empty or len(self.df) < MIN_CANDLES:
            return Signal("HOLD", 0.0, self.name)

        self.compute_indicators()
        direction = self.generate_signal()
        if direction == "HOLD":
            return Signal("HOLD", 0.0, self.name)

        curr = self.df.iloc[-1]
        score = 0.0

        # Crossover confirmed = 0.3
        score += 0.3

        # VWAP alignment = 0.25
        if direction == "BUY" and curr["close"] > curr.get("vwap", 0):
            score += 0.25
        elif direction == "SELL" and curr["close"] < curr.get("vwap", float("inf")):
            score += 0.25

        # Volume confirmation = 0.25
        vol_avg = curr.get("vol_avg", 0)
        if vol_avg > 0 and curr["volume"] > 1.5 * vol_avg:
            vol_ratio = min(curr["volume"] / vol_avg / 3.0, 1.0)
            score += 0.25 * vol_ratio

        # RSI in sweet spot = 0.2
        rsi = curr.get("rsi", 50)
        if direction == "BUY" and 45 <= rsi <= 65:
            score += 0.2
        elif direction == "SELL" and 35 <= rsi <= 55:
            score += 0.2

        return Signal(direction, min(score, 1.0), self.name)
