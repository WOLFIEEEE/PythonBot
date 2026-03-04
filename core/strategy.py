"""
Base strategy interface and advanced multi-strategy confluence engine.

Key improvements over simple voting:
  - Signal strength scoring (0.0 - 1.0) instead of binary BUY/SELL
  - Market regime detection (TRENDING / RANGING / VOLATILE)
  - Adaptive confluence thresholds based on regime
  - Detailed signal tracking for dashboard display
"""

from __future__ import annotations

from abc import ABC, abstractmethod
from dataclasses import dataclass, field
from datetime import datetime, time as dt_time
from enum import Enum
from typing import Any

import numpy as np
import pandas as pd

from config import settings
from utils.helpers import now_ist
from utils.logger import get_logger

log = get_logger(__name__)


# ═══════════════════════════════════════════════════════════════════════
#  Signal model
# ═══════════════════════════════════════════════════════════════════════
class SignalType(str, Enum):
    BUY = "BUY"
    SELL = "SELL"
    HOLD = "HOLD"


@dataclass
class Signal:
    direction: str = "HOLD"        # BUY / SELL / HOLD
    strength: float = 0.0          # 0.0 (no signal) → 1.0 (strongest)
    strategy_name: str = ""
    reason: str = ""


class BaseStrategy(ABC):
    """All strategies must inherit from this class."""

    name: str = "base"

    def __init__(self, instrument: str, candle_df: pd.DataFrame):
        self.instrument = instrument
        self.df = candle_df

    @abstractmethod
    def compute_indicators(self) -> None:
        """Calculate technical indicators on self.df in-place."""

    @abstractmethod
    def generate_signal(self) -> str:
        """Return 'BUY', 'SELL', or 'HOLD'."""

    def compute_signal_strength(self) -> Signal:
        """
        Enhanced: compute indicators, generate direction AND strength.
        Subclasses can override for custom strength scoring.
        Default: binary strength (1.0 if signal, 0.0 if hold).
        """
        if self.df.empty or len(self.df) < 2:
            return Signal("HOLD", 0.0, self.name)
        self.compute_indicators()
        direction = self.generate_signal()
        strength = 1.0 if direction != "HOLD" else 0.0
        return Signal(direction, strength, self.name)

    def evaluate(self) -> str:
        """Compute indicators then return a signal."""
        if self.df.empty or len(self.df) < 2:
            return "HOLD"
        self.compute_indicators()
        return self.generate_signal()


# ═══════════════════════════════════════════════════════════════════════
#  Market Regime Detector
# ═══════════════════════════════════════════════════════════════════════
class MarketRegime(str, Enum):
    TRENDING = "TRENDING"
    RANGING = "RANGING"
    VOLATILE = "VOLATILE"


def detect_regime(df: pd.DataFrame, lookback: int = 20) -> MarketRegime:
    """
    Classify the current market regime using ADX and ATR ratio.

    TRENDING:  ADX > 25 → strong directional movement, good for trend strategies
    VOLATILE:  ATR/close > 1.5x 50-period avg → choppy, widen SL or sit out
    RANGING:   ADX < 20 and low ATR → mean-reverting, ORB/VWAP work better
    """
    if len(df) < 50:
        return MarketRegime.RANGING

    close = df["close"]
    high = df["high"]
    low = df["low"]

    # ── ADX calculation ──
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
    current_atr = atr14.iloc[-1]
    avg_atr = atr14.iloc[-50:].mean()
    atr_ratio = current_atr / avg_atr if avg_atr > 0 else 1.0

    if pd.isna(current_adx):
        return MarketRegime.RANGING

    if atr_ratio > 1.5:
        return MarketRegime.VOLATILE
    if current_adx > 25:
        return MarketRegime.TRENDING
    return MarketRegime.RANGING


# ═══════════════════════════════════════════════════════════════════════
#  Enhanced Strategy Engine
# ═══════════════════════════════════════════════════════════════════════
class StrategyEngine:
    """
    Multi-strategy confluence with:
      - Weighted signal strength scoring
      - Market regime-adaptive thresholds
      - Detailed signal tracking for dashboard
    """

    def __init__(
        self,
        strategy_classes: list[type[BaseStrategy]],
        min_agreement: int = 2,
    ):
        self.strategy_classes = strategy_classes
        self.min_agreement = min_agreement

        # Track last signals per instrument for dashboard
        self.last_signals: dict[str, dict[str, Any]] = {}

        # Strategy weights (can be customised, updated by AdaptiveEngine)
        self._weights: dict[str, float] = {}

        # Optional reference to AdaptiveEngine for regime-aware weight boosts
        self._adaptive_engine: Any = None

    # ── Session-based strategy filtering ────────────────────────────
    def _get_active_strategies(self) -> list[type[BaseStrategy]]:
        """
        Return only the strategy classes that are active for the current
        NSE session window. Falls back to all strategies if no session matches.
        """
        session_map = getattr(settings, "SESSION_STRATEGY_MAP", None)
        if not session_map:
            return self.strategy_classes

        current = now_ist().time()
        for session_info in session_map.values():
            start_h, start_m = map(int, session_info["start"].split(":"))
            end_h, end_m = map(int, session_info["end"].split(":"))
            start = dt_time(start_h, start_m)
            end = dt_time(end_h, end_m)

            if start <= current < end:
                allowed = set(session_info["strategies"])
                active = [cls for cls in self.strategy_classes if cls.name in allowed]
                if active:
                    return active
                break  # Session matched but no strategies — fall through

        return self.strategy_classes

    def evaluate(self, instrument: str, df: pd.DataFrame) -> str:
        """
        Evaluate all strategies and return the consensus signal.

        Algorithm:
          1. Detect market regime
          2. Filter strategies by session time window
          3. Run active strategies → collect Signal objects
          4. Compute weighted score for BUY and SELL
          5. Apply regime-adaptive threshold
          6. Require min_agreement strategies to agree
          7. Return final signal
        """
        if df.empty or len(df) < 10:
            return "HOLD"

        # 1. Market regime
        regime = detect_regime(df)

        # 2. Filter by session
        active_strategies = self._get_active_strategies()

        # 3. Run strategies
        signals: list[Signal] = []
        for cls in active_strategies:
            try:
                strat = cls(instrument, df.copy())
                sig = strat.compute_signal_strength()
                signals.append(sig)
                log.debug("%s | %s → %s (%.2f)", instrument, cls.name, sig.direction, sig.strength)
            except Exception:
                log.exception("Strategy %s failed for %s", cls.name, instrument)
                signals.append(Signal("HOLD", 0.0, cls.name))

        # 3. Aggregate
        buy_signals = [s for s in signals if s.direction == "BUY"]
        sell_signals = [s for s in signals if s.direction == "SELL"]

        buy_count = len(buy_signals)
        sell_count = len(sell_signals)

        # Compute weighted scores with optional regime boost from adaptive engine
        def _effective_weight(strategy_name: str) -> float:
            base = self._weights.get(strategy_name, 1.0)
            if self._adaptive_engine:
                boost = self._adaptive_engine.get_regime_weight_boost(
                    strategy_name, regime.value,
                )
                return base * boost
            return base

        buy_score = sum(s.strength * _effective_weight(s.strategy_name) for s in buy_signals)
        sell_score = sum(s.strength * _effective_weight(s.strategy_name) for s in sell_signals)

        # 4. Regime-adaptive threshold
        threshold = self._regime_threshold(regime)
        # Scale agreement threshold to active strategy count
        # (e.g., if only 2 strategies active, don't require 3 to agree)
        effective_agreement = min(max(1, self.min_agreement), len(active_strategies))

        # In volatile regimes, require stronger consensus
        if regime == MarketRegime.VOLATILE:
            effective_agreement = min(len(active_strategies), effective_agreement + 1)

        # 5. Final decision
        final = "HOLD"
        winning_strategy = ""

        if buy_count >= effective_agreement and buy_score >= threshold:
            final = "BUY"
            winning_strategy = buy_signals[0].strategy_name if buy_signals else ""
        elif sell_count >= effective_agreement and sell_score >= threshold:
            final = "SELL"
            winning_strategy = sell_signals[0].strategy_name if sell_signals else ""

        # 6. Track for dashboard
        self.last_signals[instrument] = {
            "signal": final,
            "regime": regime.value,
            "buy_count": buy_count,
            "sell_count": sell_count,
            "buy_score": round(buy_score, 2),
            "sell_score": round(sell_score, 2),
            "threshold": round(threshold, 2),
            "effective_agreement": effective_agreement,
            "strategy": winning_strategy,
            "details": [
                {"name": s.strategy_name, "direction": s.direction, "strength": round(s.strength, 2)}
                for s in signals
            ],
            "timestamp": datetime.now().isoformat(),
        }

        if final != "HOLD":
            log.info(
                "%s | SIGNAL: %s | regime=%s | buy=%d(%.1f) sell=%d(%.1f) threshold=%.1f",
                instrument, final, regime.value,
                buy_count, buy_score, sell_count, sell_score, threshold,
            )

        return final

    @staticmethod
    def _regime_threshold(regime: MarketRegime) -> float:
        """
        Minimum weighted score needed to act on a signal.

        TRENDING: Lower bar — trends are your friend.
        RANGING:  Medium bar — false breakouts more likely.
        VOLATILE: Higher bar — noise is high, need strong consensus.
        """
        return {
            MarketRegime.TRENDING: 1.5,
            MarketRegime.RANGING: 1.8,
            MarketRegime.VOLATILE: 2.5,
        }[regime]
