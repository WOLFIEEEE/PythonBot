"""
Base strategy interface and multi-strategy confluence engine.
"""

from __future__ import annotations

from abc import ABC, abstractmethod

import pandas as pd

from utils.logger import get_logger

log = get_logger(__name__)


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

    def evaluate(self) -> str:
        """Compute indicators then return a signal."""
        if self.df.empty or len(self.df) < 2:
            return "HOLD"
        self.compute_indicators()
        return self.generate_signal()


class StrategyEngine:
    """
    Multi-strategy confluence: run several strategies and trade only when
    a majority agree.
    """

    def __init__(
        self,
        strategy_classes: list[type[BaseStrategy]],
        min_agreement: int = 2,
    ):
        self.strategy_classes = strategy_classes
        self.min_agreement = min_agreement

    def evaluate(self, instrument: str, df: pd.DataFrame) -> str:
        """
        Evaluate all strategies. Return 'BUY' or 'SELL' if at least
        `min_agreement` strategies agree, else 'HOLD'.
        """
        votes: dict[str, int] = {"BUY": 0, "SELL": 0, "HOLD": 0}
        for cls in self.strategy_classes:
            strat = cls(instrument, df.copy())
            signal = strat.evaluate()
            votes[signal] += 1
            log.debug(
                "%s | %s → %s", instrument, cls.name, signal,
            )

        if votes["BUY"] >= self.min_agreement:
            return "BUY"
        if votes["SELL"] >= self.min_agreement:
            return "SELL"
        return "HOLD"
