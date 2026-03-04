"""
Advanced market regime detection.

Extends the basic ADX + ATR regime detection with:
  1. Expiry day awareness (weekly F&O expiry — Thursdays are choppy)
  2. Day-of-week effects (Monday mornings tend to be ranging)
  3. Micro-regime detection within the trading day (open auction, lunch lull, closing drive)
  4. Volatility expansion/contraction (Bollinger Band width)
  5. Trend strength scoring (0.0 - 1.0) instead of binary classification

The base MarketRegime enum is still used for compatibility, but the
AdvancedRegimeInfo provides richer context for strategy decisions.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, time as dt_time
from enum import Enum

import numpy as np
import pandas as pd

from core.strategy import MarketRegime, detect_regime
from utils.helpers import now_ist
from utils.logger import get_logger

log = get_logger(__name__)


class MicroRegime(str, Enum):
    """Intraday micro-regime based on time of day."""
    OPEN_AUCTION = "OPEN_AUCTION"       # 9:15 - 9:30 — high vol, gap fills
    MORNING_TREND = "MORNING_TREND"     # 9:30 - 11:30 — best trending period
    LUNCH_LULL = "LUNCH_LULL"           # 11:30 - 13:00 — low volume, ranging
    AFTERNOON_MOVE = "AFTERNOON_MOVE"   # 13:00 - 14:30 — institutional flow
    CLOSING_DRIVE = "CLOSING_DRIVE"     # 14:30 - 15:30 — position squaring


class SpecialDay(str, Enum):
    """Special market days that affect volatility and strategy selection."""
    NORMAL = "NORMAL"
    WEEKLY_EXPIRY = "WEEKLY_EXPIRY"       # Thursday — F&O expiry
    MONTHLY_EXPIRY = "MONTHLY_EXPIRY"     # Last Thursday of month
    MONDAY_OPEN = "MONDAY_OPEN"           # Mondays have weekend gap risk
    FRIDAY_CLOSE = "FRIDAY_CLOSE"         # Position squaring before weekend


@dataclass
class AdvancedRegimeInfo:
    """Rich regime information for strategy decisions."""
    base_regime: MarketRegime           # TRENDING / RANGING / VOLATILE
    micro_regime: MicroRegime           # Intraday time-based regime
    special_day: SpecialDay             # Expiry / Monday / Friday etc.
    trend_strength: float               # 0.0 (no trend) to 1.0 (strong trend)
    volatility_percentile: float        # 0.0 to 1.0 — where current vol sits vs history
    bb_width_expanding: bool            # Bollinger Band width expanding (breakout likely)

    @property
    def should_widen_sl(self) -> bool:
        """Suggest wider SL during volatile or expiry periods."""
        return (
            self.base_regime == MarketRegime.VOLATILE
            or self.special_day in (SpecialDay.WEEKLY_EXPIRY, SpecialDay.MONTHLY_EXPIRY)
            or self.volatility_percentile > 0.8
        )

    @property
    def should_reduce_size(self) -> bool:
        """Suggest smaller position size during uncertain periods."""
        return (
            self.special_day == SpecialDay.MONTHLY_EXPIRY
            or self.micro_regime == MicroRegime.OPEN_AUCTION
            or (self.base_regime == MarketRegime.VOLATILE and self.volatility_percentile > 0.9)
        )

    @property
    def is_favorable_for_trend(self) -> bool:
        """Check if conditions favor trend-following strategies."""
        return (
            self.base_regime == MarketRegime.TRENDING
            and self.trend_strength > 0.5
            and self.micro_regime in (MicroRegime.MORNING_TREND, MicroRegime.AFTERNOON_MOVE)
        )

    @property
    def is_favorable_for_mean_reversion(self) -> bool:
        """Check if conditions favor mean-reversion strategies."""
        return (
            self.base_regime == MarketRegime.RANGING
            and self.micro_regime in (MicroRegime.LUNCH_LULL,)
            and not self.bb_width_expanding
        )


def detect_special_day(dt: datetime | None = None) -> SpecialDay:
    """
    Detect if today is a special trading day.

    Weekly F&O expiry: Thursday (Nifty/BankNifty weekly options expire)
    Monthly expiry: Last Thursday of the month
    """
    dt = dt or now_ist()
    weekday = dt.weekday()  # 0=Monday, 3=Thursday, 4=Friday

    if weekday == 3:  # Thursday
        # Check if it's the last Thursday of the month
        next_week = dt.day + 7
        # If adding 7 days goes past the month, this is the last Thursday
        import calendar
        _, days_in_month = calendar.monthrange(dt.year, dt.month)
        if dt.day + 7 > days_in_month:
            return SpecialDay.MONTHLY_EXPIRY
        return SpecialDay.WEEKLY_EXPIRY

    if weekday == 0:  # Monday
        return SpecialDay.MONDAY_OPEN

    if weekday == 4:  # Friday
        return SpecialDay.FRIDAY_CLOSE

    return SpecialDay.NORMAL


def detect_micro_regime(dt: datetime | None = None) -> MicroRegime:
    """Classify the current time into an intraday micro-regime."""
    dt = dt or now_ist()
    current = dt.time()

    if current < dt_time(9, 30):
        return MicroRegime.OPEN_AUCTION
    if current < dt_time(11, 30):
        return MicroRegime.MORNING_TREND
    if current < dt_time(13, 0):
        return MicroRegime.LUNCH_LULL
    if current < dt_time(14, 30):
        return MicroRegime.AFTERNOON_MOVE
    return MicroRegime.CLOSING_DRIVE


def compute_trend_strength(df: pd.DataFrame, lookback: int = 20) -> float:
    """
    Compute a normalized trend strength score (0.0 to 1.0).

    Based on:
      - ADX value (normalized to 0-1)
      - Price position relative to EMA (above = bullish strength)
      - Consecutive candle direction
    """
    if len(df) < lookback + 14:
        return 0.5

    close = df["close"]

    # 1. ADX component (0-1)
    high = df["high"]
    low = df["low"]
    plus_dm = high.diff()
    minus_dm = -low.diff()
    plus_dm = plus_dm.where((plus_dm > minus_dm) & (plus_dm > 0), 0.0)
    minus_dm = minus_dm.where((minus_dm > plus_dm) & (minus_dm > 0), 0.0)

    tr1 = high - low
    tr2 = (high - close.shift()).abs()
    tr3 = (low - close.shift()).abs()
    tr = pd.concat([tr1, tr2, tr3], axis=1).max(axis=1)
    atr14 = tr.rolling(14).mean()

    plus_di = 100 * (plus_dm.rolling(14).mean() / atr14.replace(0, np.nan))
    minus_di = 100 * (minus_dm.rolling(14).mean() / atr14.replace(0, np.nan))
    dx = (plus_di - minus_di).abs() / (plus_di + minus_di).replace(0, np.nan) * 100
    adx = dx.rolling(14).mean()

    current_adx = adx.iloc[-1]
    adx_score = min(current_adx / 50.0, 1.0) if not pd.isna(current_adx) else 0.3

    # 2. Price vs EMA component (0-1)
    ema20 = close.ewm(span=20, adjust=False).mean()
    price_vs_ema = (close.iloc[-1] - ema20.iloc[-1]) / ema20.iloc[-1] * 100
    ema_score = min(abs(price_vs_ema) / 2.0, 1.0)

    # 3. Consecutive direction component (0-1)
    recent = close.tail(lookback)
    diffs = recent.diff().dropna()
    if len(diffs) > 0:
        positive = (diffs > 0).sum()
        negative = (diffs < 0).sum()
        direction_score = abs(positive - negative) / len(diffs)
    else:
        direction_score = 0.0

    # Weighted composite
    strength = 0.5 * adx_score + 0.3 * ema_score + 0.2 * direction_score
    return round(min(max(strength, 0.0), 1.0), 3)


def compute_volatility_percentile(df: pd.DataFrame, lookback: int = 50) -> float:
    """
    Compute where current volatility sits relative to recent history.
    Returns 0.0 (lowest vol) to 1.0 (highest vol).
    """
    if len(df) < lookback + 14:
        return 0.5

    tr1 = df["high"] - df["low"]
    tr2 = (df["high"] - df["close"].shift()).abs()
    tr3 = (df["low"] - df["close"].shift()).abs()
    tr = pd.concat([tr1, tr2, tr3], axis=1).max(axis=1)
    atr = tr.rolling(14).mean()

    current_atr = atr.iloc[-1]
    historical_atrs = atr.iloc[-lookback:].dropna()

    if pd.isna(current_atr) or len(historical_atrs) < 10:
        return 0.5

    percentile = (historical_atrs < current_atr).sum() / len(historical_atrs)
    return round(percentile, 3)


def detect_bb_expansion(df: pd.DataFrame, period: int = 20, lookback: int = 5) -> bool:
    """
    Detect if Bollinger Band width is expanding (potential breakout).
    Compares current BB width to the average of the last `lookback` periods.
    """
    if len(df) < period + lookback:
        return False

    close = df["close"]
    sma = close.rolling(period).mean()
    std = close.rolling(period).std()

    bb_width = (2 * std / sma).dropna()
    if len(bb_width) < lookback + 1:
        return False

    current_width = bb_width.iloc[-1]
    recent_avg = bb_width.iloc[-lookback - 1:-1].mean()

    if pd.isna(current_width) or pd.isna(recent_avg) or recent_avg == 0:
        return False

    return current_width > recent_avg * 1.2  # 20% expansion threshold


def detect_advanced_regime(df: pd.DataFrame) -> AdvancedRegimeInfo:
    """
    Full advanced regime detection combining all signals.

    Returns an AdvancedRegimeInfo with rich context for strategy decisions.
    """
    base = detect_regime(df)
    micro = detect_micro_regime()
    special = detect_special_day()
    trend = compute_trend_strength(df)
    vol_pct = compute_volatility_percentile(df)
    bb_expanding = detect_bb_expansion(df)

    info = AdvancedRegimeInfo(
        base_regime=base,
        micro_regime=micro,
        special_day=special,
        trend_strength=trend,
        volatility_percentile=vol_pct,
        bb_width_expanding=bb_expanding,
    )

    log.debug(
        "Advanced regime: base=%s micro=%s special=%s trend=%.2f vol_pct=%.2f bb_expand=%s",
        base.value, micro.value, special.value, trend, vol_pct, bb_expanding,
    )

    return info
