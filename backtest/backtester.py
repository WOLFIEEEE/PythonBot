"""
Historical backtesting engine — replay candles through a strategy and
compute performance metrics.
"""

from __future__ import annotations

import os
from dataclasses import dataclass, field
from typing import Any

import numpy as np
import pandas as pd

from config import settings
from core.strategy import BaseStrategy
from utils.helpers import calculate_charges
from utils.logger import get_logger

log = get_logger(__name__)


@dataclass
class BacktestTrade:
    direction: str
    entry_idx: int
    entry_price: float
    exit_idx: int = 0
    exit_price: float = 0.0
    quantity: int = 1
    pnl: float = 0.0
    exit_reason: str = ""


@dataclass
class BacktestResult:
    trades: list[BacktestTrade] = field(default_factory=list)
    equity_curve: list[float] = field(default_factory=list)

    @property
    def total_trades(self) -> int:
        return len(self.trades)

    @property
    def winning(self) -> int:
        return sum(1 for t in self.trades if t.pnl > 0)

    @property
    def losing(self) -> int:
        return sum(1 for t in self.trades if t.pnl <= 0)

    @property
    def win_rate(self) -> float:
        return self.winning / self.total_trades * 100 if self.total_trades else 0

    @property
    def total_pnl(self) -> float:
        return sum(t.pnl for t in self.trades)

    @property
    def max_drawdown(self) -> float:
        if not self.equity_curve:
            return 0.0
        peak = self.equity_curve[0]
        max_dd = 0.0
        for val in self.equity_curve:
            if val > peak:
                peak = val
            dd = peak - val
            if dd > max_dd:
                max_dd = dd
        return max_dd

    @property
    def sharpe_ratio(self) -> float:
        if len(self.trades) < 2:
            return 0.0
        returns = [t.pnl for t in self.trades]
        mean = np.mean(returns)
        std = np.std(returns, ddof=1)
        if std == 0:
            return 0.0
        return float(mean / std * np.sqrt(252))

    @property
    def profit_factor(self) -> float:
        gross_profit = sum(t.pnl for t in self.trades if t.pnl > 0)
        gross_loss = abs(sum(t.pnl for t in self.trades if t.pnl < 0))
        if gross_loss == 0:
            return float("inf") if gross_profit > 0 else 0.0
        return gross_profit / gross_loss

    @property
    def avg_trade_duration(self) -> float:
        if not self.trades:
            return 0.0
        durations = [t.exit_idx - t.entry_idx for t in self.trades]
        return float(np.mean(durations))

    def summary(self) -> dict[str, Any]:
        return {
            "total_trades": self.total_trades,
            "winning": self.winning,
            "losing": self.losing,
            "win_rate": f"{self.win_rate:.1f}%",
            "total_pnl": round(self.total_pnl, 2),
            "max_drawdown": round(self.max_drawdown, 2),
            "sharpe_ratio": round(self.sharpe_ratio, 2),
            "profit_factor": round(self.profit_factor, 2),
            "avg_trade_duration_candles": round(self.avg_trade_duration, 1),
        }


class Backtester:
    """
    Replay historical candle data through a strategy.

    Usage:
        bt = Backtester(EMACrossoverStrategy, df, capital=100000)
        result = bt.run()
        print(result.summary())
    """

    def __init__(
        self,
        strategy_class: type[BaseStrategy],
        data: pd.DataFrame,
        instrument: str = "TEST",
        capital: float = settings.TOTAL_CAPITAL,
        sl_pct: float = settings.SL_PCT,
        target_pct: float = settings.TARGET_PCT,
        slippage_pct: float = settings.SLIPPAGE_PCT,
    ):
        self.strategy_class = strategy_class
        self.data = data.copy()
        self.instrument = instrument
        self.capital = capital
        self.sl_pct = sl_pct / 100
        self.target_pct = target_pct / 100
        self.slippage_pct = slippage_pct / 100

    def run(self, lookback: int = 50) -> BacktestResult:
        """Run the backtest candle by candle."""
        result = BacktestResult()
        equity = self.capital
        result.equity_curve.append(equity)

        position: BacktestTrade | None = None

        for i in range(lookback, len(self.data)):
            candle = self.data.iloc[i]

            # Check exit conditions for open position
            if position is not None:
                high = candle["high"]
                low = candle["low"]
                close = candle["close"]

                if position.direction == "BUY":
                    sl = position.entry_price * (1 - self.sl_pct)
                    tgt = position.entry_price * (1 + self.target_pct)
                    if low <= sl:
                        position.exit_price = sl * (1 - self.slippage_pct)
                        position.exit_reason = "SL_HIT"
                    elif high >= tgt:
                        position.exit_price = tgt
                        position.exit_reason = "TARGET_HIT"
                else:
                    sl = position.entry_price * (1 + self.sl_pct)
                    tgt = position.entry_price * (1 - self.target_pct)
                    if high >= sl:
                        position.exit_price = sl * (1 + self.slippage_pct)
                        position.exit_reason = "SL_HIT"
                    elif low <= tgt:
                        position.exit_price = tgt
                        position.exit_reason = "TARGET_HIT"

                if position.exit_reason:
                    position.exit_idx = i
                    if position.direction == "BUY":
                        gross = (position.exit_price - position.entry_price) * position.quantity
                    else:
                        gross = (position.entry_price - position.exit_price) * position.quantity
                    charges = calculate_charges(
                        position.entry_price, position.exit_price, position.quantity
                    )
                    position.pnl = gross - charges
                    equity += position.pnl
                    result.trades.append(position)
                    result.equity_curve.append(equity)
                    position = None
                    continue

            # No position — evaluate strategy
            if position is None:
                window = self.data.iloc[i - lookback : i + 1].copy()
                strat = self.strategy_class(self.instrument, window)
                signal = strat.evaluate()

                if signal in ("BUY", "SELL"):
                    entry_price = candle["close"]
                    if signal == "BUY":
                        entry_price *= 1 + self.slippage_pct
                        sl_price = entry_price * (1 - self.sl_pct)
                    else:
                        entry_price *= 1 - self.slippage_pct
                        sl_price = entry_price * (1 + self.sl_pct)

                    risk_per_share = abs(entry_price - sl_price)
                    risk_amount = self.capital * (settings.RISK_PER_TRADE_PCT / 100)
                    qty = max(1, int(risk_amount / risk_per_share)) if risk_per_share > 0 else 1

                    position = BacktestTrade(
                        direction=signal,
                        entry_idx=i,
                        entry_price=entry_price,
                        quantity=qty,
                    )

        # Close any remaining position at last close
        if position is not None:
            position.exit_idx = len(self.data) - 1
            position.exit_price = self.data.iloc[-1]["close"]
            position.exit_reason = "SQUARE_OFF"
            if position.direction == "BUY":
                gross = (position.exit_price - position.entry_price) * position.quantity
            else:
                gross = (position.entry_price - position.exit_price) * position.quantity
            charges = calculate_charges(
                position.entry_price, position.exit_price, position.quantity
            )
            position.pnl = gross - charges
            equity += position.pnl
            result.trades.append(position)
            result.equity_curve.append(equity)

        return result

    def print_summary(self, result: BacktestResult) -> None:
        """Pretty-print the backtest summary."""
        s = result.summary()
        print("\n" + "=" * 50)
        print(f"  Backtest: {self.strategy_class.name}")
        print(f"  Instrument: {self.instrument}")
        print(f"  Candles: {len(self.data)}")
        print("=" * 50)
        for k, v in s.items():
            print(f"  {k:30s}: {v}")
        print("=" * 50)
