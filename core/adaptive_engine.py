"""
Auto-evolving strategy engine.

Learns from its own trade history to continuously improve:
  1. Strategy weights — promote winners, demote losers
  2. Parameter evolution — nudge SL%, target%, EMA periods toward profitable zones
  3. Regime memory — learn which strategies work in which market regime
  4. Guardrails — revert on drawdown, require minimum sample size, cap drift

Uses a rolling walk-forward approach: evaluate on trailing N days,
apply changes prospectively with conservative step sizes.
"""

from __future__ import annotations

import json
import math
import os
from dataclasses import dataclass, field
from datetime import date, datetime, timedelta
from typing import Any

from config import settings
from utils.logger import get_logger

log = get_logger(__name__)

# ── Persistence file for evolution state ─────────────────────────────
EVOLUTION_STATE_FILE = os.path.join(
    os.path.dirname(os.path.dirname(__file__)),
    "data",
    "evolution_state.json",
)

# ── Safe parameter bounds (will NOT evolve outside these) ────────────
PARAM_BOUNDS: dict[str, dict[str, Any]] = {
    "SL_PCT":               {"min": 0.3,  "max": 1.5,  "step": 0.05, "type": float},
    "TARGET_PCT":            {"min": 0.5,  "max": 3.0,  "step": 0.1,  "type": float},
    "TRAILING_SL_PCT":       {"min": 0.1,  "max": 0.8,  "step": 0.05, "type": float},
    "EMA_FAST":              {"min": 5,    "max": 15,   "step": 1,    "type": int},
    "EMA_SLOW":              {"min": 15,   "max": 30,   "step": 1,    "type": int},
    "SUPERTREND_PERIOD":     {"min": 7,    "max": 14,   "step": 1,    "type": int},
    "SUPERTREND_MULTIPLIER": {"min": 2.0,  "max": 4.0,  "step": 0.25, "type": float},
}

# ── Evolution hyperparameters ────────────────────────────────────────
MIN_TRADES_FOR_EVOLUTION = 20    # Need at least N trades before adapting
ROLLING_WINDOW_DAYS = 14         # Evaluate performance over last N days
WEIGHT_LEARNING_RATE = 0.1       # How fast weights change (0=frozen, 1=instant)
PARAM_LEARNING_RATE = 0.3        # How aggressively params shift per cycle
MAX_WEIGHT = 2.0                 # No strategy can exceed 2x weight
MIN_WEIGHT = 0.3                 # No strategy below 0.3x weight
DRAWDOWN_REVERT_PCT = 5.0        # Revert all changes if drawdown > 5% of capital


@dataclass
class StrategyPerformance:
    """Rolling performance metrics for a single strategy."""
    name: str
    total_trades: int = 0
    wins: int = 0
    losses: int = 0
    total_pnl: float = 0.0
    avg_win: float = 0.0
    avg_loss: float = 0.0
    win_rate: float = 0.0
    profit_factor: float = 0.0
    # Per-regime breakdown
    regime_stats: dict[str, dict[str, float]] = field(default_factory=dict)


@dataclass
class EvolutionState:
    """Persistent state of the adaptive engine."""
    # Current strategy weights
    weights: dict[str, float] = field(default_factory=dict)
    # Current parameter overrides (evolved values)
    param_overrides: dict[str, Any] = field(default_factory=dict)
    # Baseline performance snapshot (to detect degradation)
    baseline_net_pnl: float = 0.0
    baseline_date: str = ""
    # Number of evolution cycles completed
    evolution_count: int = 0
    # Last evolution timestamp
    last_evolved: str = ""
    # Parameter history (for revert capability)
    param_history: list[dict[str, Any]] = field(default_factory=list)
    # Per-strategy per-regime performance cache
    regime_memory: dict[str, dict[str, float]] = field(default_factory=dict)


class AdaptiveEngine:
    """
    Learns from trade history and evolves strategy weights + parameters.

    Lifecycle:
      1. load_state() — restore from disk on startup
      2. record_trade() — called after each closed trade
      3. evolve() — called end-of-day to analyze and adapt
      4. save_state() — persist to disk
      5. apply_to_engine() — push weights to StrategyEngine
    """

    def __init__(self):
        self.state = EvolutionState()
        self._trade_buffer: list[dict[str, Any]] = []  # today's trades
        self._strategy_perf: dict[str, StrategyPerformance] = {}

    # ── Persistence ───────────────────────────────────────────────────

    def load_state(self) -> None:
        """Load evolution state from disk."""
        if not os.path.exists(EVOLUTION_STATE_FILE):
            log.info("No evolution state found — starting fresh.")
            return
        try:
            with open(EVOLUTION_STATE_FILE) as f:
                data = json.load(f)
            self.state = EvolutionState(
                weights=data.get("weights", {}),
                param_overrides=data.get("param_overrides", {}),
                baseline_net_pnl=data.get("baseline_net_pnl", 0.0),
                baseline_date=data.get("baseline_date", ""),
                evolution_count=data.get("evolution_count", 0),
                last_evolved=data.get("last_evolved", ""),
                param_history=data.get("param_history", []),
                regime_memory=data.get("regime_memory", {}),
            )
            log.info(
                "Evolution state loaded: %d cycles, weights=%s",
                self.state.evolution_count,
                {k: round(v, 2) for k, v in self.state.weights.items()},
            )
        except Exception as exc:
            log.error("Failed to load evolution state: %s", exc)

    def save_state(self) -> None:
        """Persist evolution state to disk."""
        os.makedirs(os.path.dirname(EVOLUTION_STATE_FILE), exist_ok=True)
        try:
            data = {
                "weights": self.state.weights,
                "param_overrides": self.state.param_overrides,
                "baseline_net_pnl": self.state.baseline_net_pnl,
                "baseline_date": self.state.baseline_date,
                "evolution_count": self.state.evolution_count,
                "last_evolved": self.state.last_evolved,
                "param_history": self.state.param_history[-30:],  # keep last 30
                "regime_memory": self.state.regime_memory,
            }
            with open(EVOLUTION_STATE_FILE, "w") as f:
                json.dump(data, f, indent=2)
            log.info("Evolution state saved.")
        except Exception as exc:
            log.error("Failed to save evolution state: %s", exc)

    # ── Trade recording ───────────────────────────────────────────────

    def record_trade(self, trade: dict[str, Any], regime: str = "UNKNOWN") -> None:
        """Record a completed trade for performance analysis."""
        trade_with_regime = dict(trade)
        trade_with_regime["regime"] = regime
        self._trade_buffer.append(trade_with_regime)

    # ── Performance analysis ──────────────────────────────────────────

    def _analyze_performance(self, trades: list[dict]) -> dict[str, StrategyPerformance]:
        """Compute per-strategy performance metrics from trade list."""
        perf: dict[str, StrategyPerformance] = {}

        for trade in trades:
            strat = trade.get("strategy", "Unknown")
            if strat not in perf:
                perf[strat] = StrategyPerformance(name=strat)

            p = perf[strat]
            pnl = trade.get("net_pnl", 0.0)
            p.total_trades += 1
            p.total_pnl += pnl

            if pnl > 0:
                p.wins += 1
            else:
                p.losses += 1

            # Track per-regime
            regime = trade.get("regime", "UNKNOWN")
            if regime not in p.regime_stats:
                p.regime_stats[regime] = {"wins": 0, "losses": 0, "pnl": 0.0}
            p.regime_stats[regime]["pnl"] += pnl
            if pnl > 0:
                p.regime_stats[regime]["wins"] += 1
            else:
                p.regime_stats[regime]["losses"] += 1

        # Compute derived metrics
        for p in perf.values():
            if p.total_trades > 0:
                p.win_rate = p.wins / p.total_trades
                win_pnls = [t["net_pnl"] for t in trades
                            if t.get("strategy") == p.name and t["net_pnl"] > 0]
                loss_pnls = [abs(t["net_pnl"]) for t in trades
                             if t.get("strategy") == p.name and t["net_pnl"] <= 0]
                p.avg_win = sum(win_pnls) / len(win_pnls) if win_pnls else 0
                p.avg_loss = sum(loss_pnls) / len(loss_pnls) if loss_pnls else 0
                total_loss = sum(loss_pnls)
                p.profit_factor = sum(win_pnls) / total_loss if total_loss > 0 else float("inf")

        return perf

    # ── Weight evolution ──────────────────────────────────────────────

    def _evolve_weights(self, perf: dict[str, StrategyPerformance]) -> dict[str, float]:
        """
        Adjust strategy weights based on rolling performance.

        Uses a composite score: 0.4 * win_rate + 0.3 * profit_factor_norm + 0.3 * avg_win/avg_loss
        Weights change slowly (learning rate) and are clamped to [MIN_WEIGHT, MAX_WEIGHT].
        """
        scores: dict[str, float] = {}

        for name, p in perf.items():
            if p.total_trades < 3:
                scores[name] = 1.0  # Not enough data — neutral weight
                continue

            # Normalize profit factor to 0-1 range (PF=2 → 1.0, PF=0.5 → 0.25)
            pf_norm = min(p.profit_factor / 2.0, 1.0) if p.profit_factor != float("inf") else 1.0

            # Win/loss ratio
            wl_ratio = (p.avg_win / p.avg_loss) if p.avg_loss > 0 else 2.0
            wl_norm = min(wl_ratio / 3.0, 1.0)

            score = 0.4 * p.win_rate + 0.3 * pf_norm + 0.3 * wl_norm
            scores[name] = score

        if not scores:
            return self.state.weights

        # Normalize scores so average = 1.0
        avg_score = sum(scores.values()) / len(scores) if scores else 1.0
        if avg_score == 0:
            avg_score = 1.0

        new_weights: dict[str, float] = {}
        for name, score in scores.items():
            target_weight = score / avg_score
            current_weight = self.state.weights.get(name, 1.0)
            # Smooth update with learning rate
            new_w = current_weight + WEIGHT_LEARNING_RATE * (target_weight - current_weight)
            # Clamp
            new_w = max(MIN_WEIGHT, min(MAX_WEIGHT, new_w))
            new_weights[name] = round(new_w, 3)

        return new_weights

    # ── Parameter evolution ───────────────────────────────────────────

    def _evolve_parameters(self, trades: list[dict]) -> dict[str, Any]:
        """
        Nudge tunable parameters toward more profitable values.

        Approach:
          - Split recent trades into "first half" and "second half"
          - If second half is worse than first, shrink SL (tighter risk)
          - If targets rarely hit, reduce TARGET_PCT
          - If SL hit rate > 60%, widen SL slightly
          - Always stay within PARAM_BOUNDS
        """
        if len(trades) < MIN_TRADES_FOR_EVOLUTION:
            return self.state.param_overrides

        overrides = dict(self.state.param_overrides)

        # Analyze SL vs target hit rates
        sl_hits = [t for t in trades if t.get("exit_reason") == "SL_HIT"]
        tgt_hits = [t for t in trades if t.get("exit_reason") == "TARGET_HIT"]
        total = len(sl_hits) + len(tgt_hits)

        if total > 0:
            sl_hit_rate = len(sl_hits) / total
            current_sl = overrides.get("SL_PCT", settings.SL_PCT)
            current_tgt = overrides.get("TARGET_PCT", settings.TARGET_PCT)
            bounds_sl = PARAM_BOUNDS["SL_PCT"]
            bounds_tgt = PARAM_BOUNDS["TARGET_PCT"]

            # SL hit rate too high (>60%) → widen SL slightly
            if sl_hit_rate > 0.60:
                new_sl = current_sl + bounds_sl["step"]
                new_sl = min(new_sl, bounds_sl["max"])
                overrides["SL_PCT"] = round(new_sl, 2)
                log.info("Evolution: SL hit rate %.0f%% — widening SL to %.2f%%", sl_hit_rate * 100, new_sl)

            # SL hit rate very low (<30%) → tighten SL to capture more
            elif sl_hit_rate < 0.30:
                new_sl = current_sl - bounds_sl["step"]
                new_sl = max(new_sl, bounds_sl["min"])
                overrides["SL_PCT"] = round(new_sl, 2)
                log.info("Evolution: SL hit rate %.0f%% — tightening SL to %.2f%%", sl_hit_rate * 100, new_sl)

            # Target rarely hit (<25%) → reduce target to be more achievable
            tgt_hit_rate = len(tgt_hits) / total if total > 0 else 0
            if tgt_hit_rate < 0.25:
                new_tgt = current_tgt - bounds_tgt["step"]
                new_tgt = max(new_tgt, bounds_tgt["min"])
                overrides["TARGET_PCT"] = round(new_tgt, 2)
                log.info("Evolution: Target hit rate %.0f%% — reducing target to %.2f%%", tgt_hit_rate * 100, new_tgt)

        # Analyze overall P&L trend
        if len(trades) >= 10:
            first_half = trades[:len(trades)//2]
            second_half = trades[len(trades)//2:]
            first_pnl = sum(t.get("net_pnl", 0) for t in first_half)
            second_pnl = sum(t.get("net_pnl", 0) for t in second_half)

            # If performance is degrading, adjust trailing SL
            if second_pnl < first_pnl * 0.5 and second_pnl < 0:
                current_trail = overrides.get("TRAILING_SL_PCT", settings.TRAILING_SL_PCT)
                bounds_trail = PARAM_BOUNDS["TRAILING_SL_PCT"]
                # Tighten trailing SL to lock in profits faster
                new_trail = current_trail - bounds_trail["step"]
                new_trail = max(new_trail, bounds_trail["min"])
                overrides["TRAILING_SL_PCT"] = round(new_trail, 2)
                log.info(
                    "Evolution: Performance degrading (%.0f → %.0f) — "
                    "tightening trailing SL to %.2f%%",
                    first_pnl, second_pnl, new_trail,
                )

        return overrides

    # ── Regime memory ─────────────────────────────────────────────────

    def _update_regime_memory(self, perf: dict[str, StrategyPerformance]) -> None:
        """
        Build a memory of which strategies perform best in each regime.
        Used to dynamically adjust weights mid-session.
        """
        for name, p in perf.items():
            for regime, stats in p.regime_stats.items():
                key = f"{name}:{regime}"
                total = stats["wins"] + stats["losses"]
                if total < 3:
                    continue
                win_rate = stats["wins"] / total
                self.state.regime_memory[key] = round(win_rate, 3)

    def get_regime_weight_boost(self, strategy_name: str, regime: str) -> float:
        """
        Return a weight multiplier based on regime memory.
        Strategies that historically do well in this regime get boosted.
        """
        key = f"{strategy_name}:{regime}"
        historical_wr = self.state.regime_memory.get(key)
        if historical_wr is None:
            return 1.0  # No data — neutral
        # Scale: 50% win rate → 1.0x, 70% → 1.4x, 30% → 0.6x
        return max(0.5, min(1.5, historical_wr * 2.0))

    # ── Guardrails ────────────────────────────────────────────────────

    def _check_guardrails(self, trades: list[dict]) -> bool:
        """
        Check if evolved parameters are causing harm.
        Returns True if we should REVERT to defaults.
        """
        if not trades or len(trades) < 5:
            return False

        # Check rolling drawdown
        cumulative = 0.0
        peak = 0.0
        max_dd = 0.0
        for t in trades:
            cumulative += t.get("net_pnl", 0)
            peak = max(peak, cumulative)
            dd = peak - cumulative
            max_dd = max(max_dd, dd)

        revert_threshold = settings.TOTAL_CAPITAL * DRAWDOWN_REVERT_PCT / 100
        if max_dd > revert_threshold:
            log.warning(
                "GUARDRAIL TRIGGERED: Drawdown %.0f > %.0f threshold — "
                "reverting all evolved parameters to defaults.",
                max_dd, revert_threshold,
            )
            return True

        # Check if evolved params are significantly worse than baseline
        if self.state.baseline_net_pnl > 0:
            recent_pnl = sum(t.get("net_pnl", 0) for t in trades[-10:])
            if recent_pnl < -self.state.baseline_net_pnl * 0.5:
                log.warning(
                    "GUARDRAIL: Recent P&L (%.0f) significantly worse than "
                    "baseline (%.0f) — reverting.",
                    recent_pnl, self.state.baseline_net_pnl,
                )
                return True

        return False

    def _revert_to_defaults(self) -> None:
        """Reset all evolved parameters and weights to defaults."""
        log.warning("Reverting all evolution changes to defaults.")
        # Save current as history before reverting
        if self.state.param_overrides:
            self.state.param_history.append({
                "date": date.today().isoformat(),
                "reverted": True,
                "params": dict(self.state.param_overrides),
                "weights": dict(self.state.weights),
            })
        self.state.weights = {}
        self.state.param_overrides = {}

    # ── Main evolution cycle ──────────────────────────────────────────

    def evolve(self, db_trades: list[dict] | None = None) -> dict[str, Any]:
        """
        Run one evolution cycle. Called end-of-day.

        Returns a summary dict of what changed.

        Args:
            db_trades: Historical trades from DB. If None, uses today's buffer.
        """
        trades = db_trades or self._trade_buffer

        if len(trades) < MIN_TRADES_FOR_EVOLUTION:
            log.info(
                "Not enough trades for evolution (%d < %d). Skipping.",
                len(trades), MIN_TRADES_FOR_EVOLUTION,
            )
            return {"evolved": False, "reason": "insufficient_trades"}

        # 1. Check guardrails first
        if self._check_guardrails(trades):
            self._revert_to_defaults()
            self.save_state()
            return {"evolved": True, "action": "REVERTED_TO_DEFAULTS"}

        # 2. Analyze per-strategy performance
        perf = self._analyze_performance(trades)
        self._strategy_perf = perf

        # 3. Evolve weights
        old_weights = dict(self.state.weights)
        new_weights = self._evolve_weights(perf)
        self.state.weights = new_weights

        # 4. Evolve parameters
        old_params = dict(self.state.param_overrides)
        new_params = self._evolve_parameters(trades)
        self.state.param_overrides = new_params

        # 5. Update regime memory
        self._update_regime_memory(perf)

        # 6. Set baseline if first evolution
        if self.state.evolution_count == 0:
            self.state.baseline_net_pnl = sum(t.get("net_pnl", 0) for t in trades)
            self.state.baseline_date = date.today().isoformat()

        # 7. Record history
        self.state.param_history.append({
            "date": date.today().isoformat(),
            "reverted": False,
            "params": dict(new_params),
            "weights": dict(new_weights),
        })

        self.state.evolution_count += 1
        self.state.last_evolved = datetime.now().isoformat()

        # 8. Persist
        self.save_state()

        # 9. Build summary
        weight_changes = {
            k: {"old": round(old_weights.get(k, 1.0), 3), "new": round(v, 3)}
            for k, v in new_weights.items()
            if abs(old_weights.get(k, 1.0) - v) > 0.01
        }
        param_changes = {
            k: {"old": old_params.get(k, getattr(settings, k, "?")), "new": v}
            for k, v in new_params.items()
            if old_params.get(k, getattr(settings, k, None)) != v
        }

        summary = {
            "evolved": True,
            "cycle": self.state.evolution_count,
            "trades_analyzed": len(trades),
            "strategy_performance": {
                name: {
                    "trades": p.total_trades,
                    "win_rate": round(p.win_rate, 2),
                    "profit_factor": round(p.profit_factor, 2),
                    "total_pnl": round(p.total_pnl, 2),
                }
                for name, p in perf.items()
            },
            "weight_changes": weight_changes,
            "param_changes": param_changes,
        }

        log.info(
            "Evolution cycle #%d complete: %d trades analyzed, "
            "%d weight changes, %d param changes.",
            self.state.evolution_count,
            len(trades),
            len(weight_changes),
            len(param_changes),
        )

        return summary

    # ── Apply to live engine ──────────────────────────────────────────

    def apply_to_engine(self, strategy_engine: Any) -> None:
        """Push evolved weights to the StrategyEngine."""
        if self.state.weights:
            strategy_engine._weights = dict(self.state.weights)
            log.info("Applied evolved weights to strategy engine: %s",
                     {k: round(v, 2) for k, v in self.state.weights.items()})

    def apply_param_overrides(self) -> None:
        """
        Apply evolved parameter overrides to the settings module.
        Only changes values within PARAM_BOUNDS — never touches credentials
        or critical infrastructure settings.
        """
        for key, value in self.state.param_overrides.items():
            if key not in PARAM_BOUNDS:
                continue  # Safety: only evolve known parameters
            bounds = PARAM_BOUNDS[key]
            # Enforce bounds one more time
            clamped = max(bounds["min"], min(bounds["max"], value))
            clamped = bounds["type"](clamped)
            setattr(settings, key, clamped)
            log.info("Applied evolved param: %s = %s", key, clamped)

    # ── Fetch historical trades from DB ───────────────────────────────

    @staticmethod
    def fetch_recent_trades(days: int = ROLLING_WINDOW_DAYS) -> list[dict]:
        """Load trades from the last N days from the database."""
        from utils.db import TradeLog, get_session
        cutoff = date.today() - timedelta(days=days)
        session = get_session()
        try:
            rows = (
                session.query(TradeLog)
                .filter(TradeLog.date >= cutoff)
                .order_by(TradeLog.exit_time)
                .all()
            )
            return [
                {
                    "instrument": r.instrument,
                    "direction": r.direction,
                    "entry_price": r.entry_price,
                    "exit_price": r.exit_price,
                    "quantity": r.quantity,
                    "net_pnl": r.pnl,
                    "strategy": r.strategy,
                    "exit_reason": r.exit_reason,
                    "date": r.date.isoformat() if r.date else "",
                    "regime": "UNKNOWN",  # DB doesn't store regime yet
                }
                for r in rows
            ]
        finally:
            session.close()

    # ── Flush daily buffer ────────────────────────────────────────────

    def flush_daily_buffer(self) -> None:
        """Clear today's trade buffer (called after evolution)."""
        self._trade_buffer.clear()

    # ── Status for dashboard / logging ────────────────────────────────

    @property
    def status(self) -> dict[str, Any]:
        return {
            "evolution_count": self.state.evolution_count,
            "last_evolved": self.state.last_evolved,
            "current_weights": dict(self.state.weights),
            "param_overrides": dict(self.state.param_overrides),
            "trades_today": len(self._trade_buffer),
            "regime_memory_entries": len(self.state.regime_memory),
        }
