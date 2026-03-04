"""
Track open positions, unrealized/realized P&L, and reconcile with Kite.
Thread-safe with locking for concurrent access from WebSocket and scheduler.
"""

from __future__ import annotations

import threading
from dataclasses import dataclass, field
from datetime import datetime
from typing import Any

from kiteconnect import KiteConnect

from config import settings
from utils.helpers import calculate_charges, now_ist, retry
from utils.logger import get_logger

log = get_logger(__name__)


@dataclass
class Position:
    instrument: str
    direction: str                    # BUY or SELL
    entry_price: float
    quantity: int
    sl_order_id: str | None = None
    target_order_id: str | None = None
    entry_order_id: str | None = None
    entry_time: datetime = field(default_factory=now_ist)
    strategy: str = ""
    sl_price: float = 0.0
    target_price: float = 0.0
    trailing_sl: float = 0.0
    current_pnl: float = 0.0
    # Scaled entry tracking
    total_planned_qty: int = 0       # Full intended quantity
    scale_in_remaining: int = 0      # Shares still to add
    scale_in_candles_waited: int = 0 # Candles since entry (for timing add-ons)
    scale_in_complete: bool = True   # True if no more add-ons needed


class PositionTracker:
    """In-memory position tracker with periodic Kite reconciliation."""

    def __init__(self, kite: KiteConnect):
        self.kite = kite
        self._lock = threading.Lock()
        self.positions: dict[str, Position] = {}
        self.total_realized_pnl: float = 0.0
        self.total_unrealized_pnl: float = 0.0
        self.closed_trades: list[dict[str, Any]] = []
        # Reserved slots: symbols with pending entry orders (atomic position count)
        self._reserved_slots: set[str] = set()

    # ── Atomic slot reservation ───────────────────────────────────────
    def reserve_slot(self, symbol: str) -> bool:
        """
        Atomically reserve a position slot before placing the entry order.
        Returns False if max positions reached or symbol already reserved/open.
        This prevents the race condition where two instruments pass the
        MAX_OPEN_POSITIONS check simultaneously.
        """
        with self._lock:
            if symbol in self.positions or symbol in self._reserved_slots:
                return False
            total = len(self.positions) + len(self._reserved_slots)
            if total >= settings.MAX_OPEN_POSITIONS:
                return False
            self._reserved_slots.add(symbol)
            return True

    def release_slot(self, symbol: str) -> None:
        """Release a reserved slot (e.g., if entry order fails/rejected)."""
        with self._lock:
            self._reserved_slots.discard(symbol)

    # ── Open / close ─────────────────────────────────────────────────
    def open_position(
        self,
        symbol: str,
        direction: str,
        entry_price: float,
        quantity: int,
        sl_price: float,
        target_price: float,
        strategy: str,
        entry_order_id: str | None = None,
        sl_order_id: str | None = None,
        target_order_id: str | None = None,
    ) -> Position:
        with self._lock:
            pos = Position(
                instrument=symbol,
                direction=direction,
                entry_price=entry_price,
                quantity=quantity,
                sl_price=sl_price,
                target_price=target_price,
                trailing_sl=sl_price,
                strategy=strategy,
                entry_order_id=entry_order_id,
                sl_order_id=sl_order_id,
                target_order_id=target_order_id,
            )
            self.positions[symbol] = pos
            # Clear reserved slot — now a real position
            self._reserved_slots.discard(symbol)
        log.info(
            "Position opened: %s %s @ %.2f qty=%d SL=%.2f TGT=%.2f",
            direction, symbol, entry_price, quantity, sl_price, target_price,
        )
        return pos

    def close_position(
        self,
        symbol: str,
        exit_price: float,
        exit_reason: str,
    ) -> dict[str, Any] | None:
        with self._lock:
            pos = self.positions.pop(symbol, None)
        if pos is None:
            log.warning("Tried to close non-existent position: %s", symbol)
            return None

        exit_time = now_ist()
        if pos.direction == "BUY":
            gross_pnl = (exit_price - pos.entry_price) * pos.quantity
        else:
            gross_pnl = (pos.entry_price - exit_price) * pos.quantity

        charges = calculate_charges(pos.entry_price, exit_price, pos.quantity)
        net_pnl = gross_pnl - charges

        with self._lock:
            self.total_realized_pnl += net_pnl

        duration_min = int((exit_time - pos.entry_time).total_seconds() / 60)

        trade_record = {
            "instrument": symbol,
            "direction": pos.direction,
            "entry_price": pos.entry_price,
            "exit_price": exit_price,
            "quantity": pos.quantity,
            "entry_time": pos.entry_time,
            "exit_time": exit_time,
            "gross_pnl": gross_pnl,
            "net_pnl": net_pnl,
            "charges": charges,
            "strategy": pos.strategy,
            "sl_price": pos.sl_price,
            "target_price": pos.target_price,
            "exit_reason": exit_reason,
            "duration_min": duration_min,
            "entry_order_id": pos.entry_order_id,
            "sl_order_id": pos.sl_order_id,
            "target_order_id": pos.target_order_id,
        }

        with self._lock:
            self.closed_trades.append(trade_record)

        log.info(
            "Position closed: %s %s exit=%.2f pnl=%.2f reason=%s",
            pos.direction, symbol, exit_price, net_pnl, exit_reason,
        )
        return trade_record

    # ── Scale-in: add shares to existing position ──────────────────
    def add_to_position(
        self,
        symbol: str,
        additional_qty: int,
        fill_price: float,
    ) -> bool:
        """
        Add shares to an existing position (scale-in).
        Updates average entry price and quantity.
        Returns True if successful.
        """
        with self._lock:
            pos = self.positions.get(symbol)
            if pos is None:
                return False

            old_cost = pos.entry_price * pos.quantity
            new_cost = fill_price * additional_qty
            new_qty = pos.quantity + additional_qty
            pos.entry_price = round((old_cost + new_cost) / new_qty, 2)
            pos.quantity = new_qty
            pos.scale_in_remaining = max(0, pos.scale_in_remaining - additional_qty)
            if pos.scale_in_remaining == 0:
                pos.scale_in_complete = True

        log.info(
            "Scale-in: %s %s +%d shares @ %.2f — total qty=%d avg=%.2f",
            pos.direction, symbol, additional_qty, fill_price, new_qty, pos.entry_price,
        )
        return True

    def increment_scale_candle(self, symbol: str) -> int:
        """Increment the scale-in candle counter. Returns new count."""
        with self._lock:
            pos = self.positions.get(symbol)
            if pos is None:
                return 0
            pos.scale_in_candles_waited += 1
            return pos.scale_in_candles_waited

    # ── Update unrealized P&L from live prices ───────────────────────
    def update_unrealized_pnl(self, live_prices: dict[str, float]) -> None:
        with self._lock:
            total = 0.0
            for symbol, pos in self.positions.items():
                ltp = live_prices.get(symbol)
                if ltp is None:
                    continue
                if pos.direction == "BUY":
                    pos.current_pnl = (ltp - pos.entry_price) * pos.quantity
                else:
                    pos.current_pnl = (pos.entry_price - ltp) * pos.quantity
                total += pos.current_pnl
            self.total_unrealized_pnl = total

    # ── Revert trailing SL (if exchange modify fails) ────────────────
    def revert_trailing_sl(self, symbol: str, old_sl: float) -> None:
        """Revert trailing_sl to old value when exchange SL modify fails."""
        with self._lock:
            pos = self.positions.get(symbol)
            if pos is not None:
                pos.trailing_sl = old_sl

    # ── Trailing stop-loss update ────────────────────────────────────
    def update_trailing_sl(self, symbol: str, ltp: float) -> float | None:
        """
        Update trailing SL if price has moved in favour. Returns new SL
        trigger if changed, else None.
        """
        with self._lock:
            pos = self.positions.get(symbol)
            if pos is None or not settings.TRAILING_SL:
                return None

            trail = settings.TRAILING_SL_PCT / 100
            if pos.direction == "BUY":
                new_sl = ltp * (1 - trail)
                if new_sl > pos.trailing_sl:
                    pos.trailing_sl = round(new_sl, 2)
                    return pos.trailing_sl
            else:
                new_sl = ltp * (1 + trail)
                if pos.trailing_sl == 0 or new_sl < pos.trailing_sl:
                    pos.trailing_sl = round(new_sl, 2)
                    return pos.trailing_sl
        return None

    # ── Reconcile with Kite positions ────────────────────────────────
    @retry(max_retries=2, exceptions=(Exception,))
    def sync_with_kite(self) -> list[str]:
        """
        Fetch positions from Kite and reconcile.
        Returns list of symbols closed externally (qty=0 in Kite).
        """
        externally_closed: list[str] = []
        try:
            kite_positions = self.kite.positions().get("net", [])
        except Exception as exc:
            log.error("Position sync failed: %s", exc)
            return externally_closed

        kite_map: dict[str, int] = {}
        for p in kite_positions:
            if p.get("product") == settings.PRODUCT_TYPE:
                kite_map[p["tradingsymbol"]] = p.get("quantity", 0)

        with self._lock:
            for symbol in list(self.positions.keys()):
                kite_qty = kite_map.get(symbol, 0)
                if kite_qty == 0:
                    log.warning(
                        "Kite shows zero qty for %s — closed externally.",
                        symbol,
                    )
                    externally_closed.append(symbol)

        return externally_closed

    # ── Get position snapshot (thread-safe) ──────────────────────────
    def get_open_symbols(self) -> list[str]:
        with self._lock:
            return list(self.positions.keys())

    def get_position(self, symbol: str) -> Position | None:
        with self._lock:
            return self.positions.get(symbol)

    # ── Summary helpers ──────────────────────────────────────────────
    @property
    def daily_summary(self) -> dict[str, Any]:
        with self._lock:
            trades = list(self.closed_trades)
        wins = [t for t in trades if t["net_pnl"] > 0]
        losses = [t for t in trades if t["net_pnl"] <= 0]
        gross = sum(t["gross_pnl"] for t in trades)
        net = sum(t["net_pnl"] for t in trades)
        max_dd = self._max_drawdown(trades)
        capital_used = sum(t["entry_price"] * t["quantity"] for t in trades)
        return {
            "total_trades": len(trades),
            "winning_trades": len(wins),
            "losing_trades": len(losses),
            "gross_pnl": gross,
            "net_pnl": net,
            "max_drawdown": max_dd,
            "capital_used": capital_used,
        }

    @staticmethod
    def _max_drawdown(trades: list[dict[str, Any]]) -> float:
        if not trades:
            return 0.0
        cumulative = 0.0
        peak = 0.0
        max_dd = 0.0
        for t in trades:
            cumulative += t["net_pnl"]
            if cumulative > peak:
                peak = cumulative
            dd = peak - cumulative
            if dd > max_dd:
                max_dd = dd
        return max_dd
