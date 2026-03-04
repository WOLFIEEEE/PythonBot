"""
Risk management — position sizing, pre-trade checks, circuit breakers.
"""

from __future__ import annotations

from datetime import datetime
from typing import TYPE_CHECKING

from config import settings
from utils.helpers import is_past_no_new_trades_time, now_ist, retry
from utils.logger import get_logger
from utils.notifier import notify_risk_breach

if TYPE_CHECKING:
    from kiteconnect import KiteConnect

    from core.position_tracker import PositionTracker

log = get_logger(__name__)


class RiskManager:
    """Pre-trade gatekeeper and intraday risk controls."""

    def __init__(self, kite: KiteConnect, capital: float = settings.TOTAL_CAPITAL):
        self.kite = kite
        self.starting_capital = capital
        self.trades_today = 0
        self.consecutive_losses = 0
        self._paused_until: datetime | None = None

    # ── Position sizing ──────────────────────────────────────────────
    @staticmethod
    def calculate_quantity(
        capital: float,
        risk_pct: float,
        entry_price: float,
        sl_price: float,
    ) -> int:
        risk_amount = capital * (risk_pct / 100)
        risk_per_share = abs(entry_price - sl_price)
        if risk_per_share <= 0:
            return 1
        qty = int(risk_amount / risk_per_share)
        return max(1, qty)

    # ── Pre-trade checks ─────────────────────────────────────────────
    def pre_trade_checks(
        self,
        symbol: str,
        position_tracker: PositionTracker,
        entry_price: float,
        sl_price: float,
    ) -> tuple[bool, str]:
        """
        Run all pre-trade validations.
        Returns (passed: bool, reason: str).
        """
        # 1 — Daily loss limit
        daily_pnl = position_tracker.total_realized_pnl + position_tracker.total_unrealized_pnl
        max_loss = self.starting_capital * settings.MAX_DAILY_LOSS_PCT / 100
        if daily_pnl < 0 and abs(daily_pnl) >= max_loss:
            msg = f"Daily loss limit breached (P&L={daily_pnl:.0f}, limit=-{max_loss:.0f})"
            log.warning(msg)
            notify_risk_breach(msg)
            return False, msg

        # 2 — Max trades per day
        if self.trades_today >= settings.MAX_TRADES_PER_DAY:
            return False, f"Max trades ({settings.MAX_TRADES_PER_DAY}) reached."

        # 3 — Max open positions
        if len(position_tracker.positions) >= settings.MAX_OPEN_POSITIONS:
            return False, f"Max open positions ({settings.MAX_OPEN_POSITIONS}) reached."

        # 4 — Time gate
        if is_past_no_new_trades_time():
            return False, f"Past no-new-trades cutoff ({settings.NO_NEW_TRADES_AFTER})."

        # 5 — Margin check
        if not self._has_sufficient_margin(entry_price, sl_price):
            return False, "Insufficient margin."

        # 6 — No duplicate position
        if symbol in position_tracker.positions:
            return False, f"Already in a position for {symbol}."

        # 7 — Consecutive-loss pause
        if self._paused_until and now_ist() < self._paused_until:
            return False, f"Paused until {self._paused_until.strftime('%H:%M')} (consecutive losses)."

        return True, "OK"

    # ── Margin helper ────────────────────────────────────────────────
    @retry(max_retries=2, exceptions=(Exception,))
    def _has_sufficient_margin(self, entry_price: float, sl_price: float) -> bool:
        qty = self.calculate_quantity(
            self.starting_capital,
            settings.RISK_PER_TRADE_PCT,
            entry_price,
            sl_price,
        )
        required = entry_price * qty * 0.25  # rough MIS margin ~25%
        try:
            margins = self.kite.margins("equity")
            available = margins.get("available", {}).get("live_balance", 0)
            return available >= required
        except Exception:
            log.warning("Margin check failed — allowing trade cautiously.")
            return True

    # ── Record trade outcome ─────────────────────────────────────────
    def record_trade(self, pnl: float) -> None:
        self.trades_today += 1
        if pnl < 0:
            self.consecutive_losses += 1
            if self.consecutive_losses >= settings.CONSECUTIVE_LOSS_PAUSE_THRESHOLD:
                pause_until = now_ist().replace(second=0, microsecond=0)
                from datetime import timedelta
                pause_until += timedelta(minutes=settings.CONSECUTIVE_LOSS_PAUSE_MINUTES)
                self._paused_until = pause_until
                msg = (
                    f"{self.consecutive_losses} consecutive losses — "
                    f"pausing until {pause_until.strftime('%H:%M')}"
                )
                log.warning(msg)
                notify_risk_breach(msg)
        else:
            self.consecutive_losses = 0

    # ── Daily loss circuit breaker ───────────────────────────────────
    def should_stop_trading(self, position_tracker: PositionTracker) -> bool:
        daily_pnl = position_tracker.total_realized_pnl + position_tracker.total_unrealized_pnl
        max_loss = self.starting_capital * settings.MAX_DAILY_LOSS_PCT / 100
        if daily_pnl < 0 and abs(daily_pnl) >= max_loss:
            return True
        return False
