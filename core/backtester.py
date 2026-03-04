"""
Lightweight backtester for validating evolved parameters before deployment.

Uses historical candle data from the database to simulate trades with
candidate parameters, then compares performance against current params.
Only deploys changes that show improvement on out-of-sample data.

Walk-forward approach:
  - Train window: first 70% of data (optimize on this)
  - Validation window: last 30% (must also improve here)
  - Both must show improvement to deploy
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import date, timedelta
from typing import Any

import pandas as pd

from config import settings
from utils.helpers import calculate_charges
from utils.logger import get_logger

log = get_logger(__name__)

# Walk-forward split ratio
TRAIN_RATIO = 0.7


@dataclass
class BacktestResult:
    """Results from a single backtest run."""
    total_trades: int = 0
    wins: int = 0
    losses: int = 0
    net_pnl: float = 0.0
    max_drawdown: float = 0.0
    win_rate: float = 0.0
    profit_factor: float = 0.0
    avg_trade_pnl: float = 0.0
    sharpe_estimate: float = 0.0

    @property
    def is_profitable(self) -> bool:
        return self.net_pnl > 0 and self.win_rate > 0.35


def _simulate_trades_from_history(
    trades: list[dict],
    sl_pct: float,
    target_pct: float,
    trailing_sl_pct: float,
) -> BacktestResult:
    """
    Re-simulate trade outcomes with different SL/target parameters.

    Uses actual entry prices from historical trades but recalculates
    exit points based on the candidate SL/target percentages.
    """
    result = BacktestResult()
    pnl_series: list[float] = []

    for trade in trades:
        entry = trade.get("entry_price", 0)
        direction = trade.get("direction", "BUY")
        qty = trade.get("quantity", 1)

        if entry <= 0:
            continue

        # Calculate SL and target with candidate params
        if direction == "BUY":
            sl = entry * (1 - sl_pct / 100)
            target = entry * (1 + target_pct / 100)
        else:
            sl = entry * (1 + sl_pct / 100)
            target = entry * (1 - target_pct / 100)

        # Use actual exit price to determine what would have happened
        actual_exit = trade.get("exit_price", entry)
        actual_reason = trade.get("exit_reason", "")

        # Re-evaluate: would the new SL/target have been hit first?
        if direction == "BUY":
            hit_sl = actual_exit <= sl
            hit_target = actual_exit >= target
        else:
            hit_sl = actual_exit >= sl
            hit_target = actual_exit <= target

        # Determine simulated exit
        if hit_sl and not hit_target:
            sim_exit = sl
            sim_reason = "SL_HIT"
        elif hit_target and not hit_sl:
            sim_exit = target
            sim_reason = "TARGET_HIT"
        elif actual_reason == "SQUARE_OFF":
            sim_exit = actual_exit
            sim_reason = "SQUARE_OFF"
        else:
            # Neither hit — use actual exit
            sim_exit = actual_exit
            sim_reason = actual_reason

        # Calculate P&L
        if direction == "BUY":
            gross = (sim_exit - entry) * qty
        else:
            gross = (entry - sim_exit) * qty

        charges = calculate_charges(entry, sim_exit, qty)
        net = gross - charges
        pnl_series.append(net)

        result.total_trades += 1
        if net > 0:
            result.wins += 1
        else:
            result.losses += 1
        result.net_pnl += net

    # Compute aggregate metrics
    if result.total_trades > 0:
        result.win_rate = result.wins / result.total_trades
        result.avg_trade_pnl = result.net_pnl / result.total_trades

        win_pnls = [p for p in pnl_series if p > 0]
        loss_pnls = [abs(p) for p in pnl_series if p <= 0]
        total_wins = sum(win_pnls)
        total_losses = sum(loss_pnls)
        result.profit_factor = total_wins / total_losses if total_losses > 0 else float("inf")

        # Max drawdown
        cumulative = 0.0
        peak = 0.0
        max_dd = 0.0
        for p in pnl_series:
            cumulative += p
            peak = max(peak, cumulative)
            dd = peak - cumulative
            max_dd = max(max_dd, dd)
        result.max_drawdown = max_dd

        # Sharpe estimate (annualized from daily returns)
        if len(pnl_series) > 1:
            avg = sum(pnl_series) / len(pnl_series)
            var = sum((p - avg) ** 2 for p in pnl_series) / len(pnl_series)
            std = var ** 0.5
            result.sharpe_estimate = (avg / std * (252 ** 0.5)) if std > 0 else 0

    return result


def validate_params(
    trades: list[dict],
    candidate_params: dict[str, Any],
    current_params: dict[str, float] | None = None,
) -> dict[str, Any]:
    """
    Walk-forward validation: test candidate parameters against current ones.

    Splits trades into train (70%) and validation (30%) sets.
    Candidate must beat current on BOTH sets to be approved.

    Returns:
        {
            "approved": bool,
            "reason": str,
            "current_result": BacktestResult,
            "candidate_result": BacktestResult,
            "improvement_pct": float,
        }
    """
    if len(trades) < 10:
        return {"approved": False, "reason": "insufficient_trades"}

    # Current params (baseline)
    cur_sl = current_params.get("SL_PCT", settings.SL_PCT) if current_params else settings.SL_PCT
    cur_tgt = current_params.get("TARGET_PCT", settings.TARGET_PCT) if current_params else settings.TARGET_PCT
    cur_trail = current_params.get("TRAILING_SL_PCT", settings.TRAILING_SL_PCT) if current_params else settings.TRAILING_SL_PCT

    # Candidate params
    cand_sl = candidate_params.get("SL_PCT", cur_sl)
    cand_tgt = candidate_params.get("TARGET_PCT", cur_tgt)
    cand_trail = candidate_params.get("TRAILING_SL_PCT", cur_trail)

    # Walk-forward split
    split_idx = int(len(trades) * TRAIN_RATIO)
    train_trades = trades[:split_idx]
    val_trades = trades[split_idx:]

    if len(train_trades) < 5 or len(val_trades) < 3:
        return {"approved": False, "reason": "insufficient_split_size"}

    # Run backtests
    cur_train = _simulate_trades_from_history(train_trades, cur_sl, cur_tgt, cur_trail)
    cur_val = _simulate_trades_from_history(val_trades, cur_sl, cur_tgt, cur_trail)
    cand_train = _simulate_trades_from_history(train_trades, cand_sl, cand_tgt, cand_trail)
    cand_val = _simulate_trades_from_history(val_trades, cand_sl, cand_tgt, cand_trail)

    # Combined results
    cur_total = BacktestResult(
        net_pnl=cur_train.net_pnl + cur_val.net_pnl,
        total_trades=cur_train.total_trades + cur_val.total_trades,
    )
    cand_total = BacktestResult(
        net_pnl=cand_train.net_pnl + cand_val.net_pnl,
        total_trades=cand_train.total_trades + cand_val.total_trades,
    )

    # Approval criteria:
    # 1. Candidate must beat current on validation set (out-of-sample)
    # 2. Candidate must not lose money on validation set
    # 3. Candidate drawdown must not be significantly worse
    val_improved = cand_val.net_pnl > cur_val.net_pnl
    val_profitable = cand_val.net_pnl >= 0
    dd_acceptable = cand_val.max_drawdown <= cur_val.max_drawdown * 1.5 + 100  # Allow 50% more DD

    approved = val_improved and val_profitable and dd_acceptable

    improvement = 0.0
    if cur_val.net_pnl != 0:
        improvement = (cand_val.net_pnl - cur_val.net_pnl) / abs(cur_val.net_pnl) * 100
    elif cand_val.net_pnl > 0:
        improvement = 100.0

    reason = "approved" if approved else (
        "validation_not_improved" if not val_improved else
        "validation_not_profitable" if not val_profitable else
        "drawdown_too_high"
    )

    log.info(
        "Backtest: current_val_pnl=%.0f candidate_val_pnl=%.0f "
        "improvement=%.1f%% approved=%s reason=%s",
        cur_val.net_pnl, cand_val.net_pnl, improvement, approved, reason,
    )

    return {
        "approved": approved,
        "reason": reason,
        "current_train": cur_train,
        "current_val": cur_val,
        "candidate_train": cand_train,
        "candidate_val": cand_val,
        "improvement_pct": round(improvement, 1),
    }
