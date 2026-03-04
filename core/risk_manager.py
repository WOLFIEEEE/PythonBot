"""
Risk management — position sizing, pre-trade checks, circuit breakers,
volatility filter, exposure caps.
"""

from __future__ import annotations

from datetime import datetime, timedelta
from typing import TYPE_CHECKING

import pandas as pd

from config import settings
from utils.helpers import is_past_no_new_trades_time, now_ist, retry
from utils.logger import get_logger
from utils.notifier import notify_risk_breach

if TYPE_CHECKING:
    from kiteconnect import KiteConnect

    from core.position_tracker import PositionTracker

log = get_logger(__name__)

# Maximum percentage of capital that can be deployed across all positions
MAX_TOTAL_EXPOSURE_PCT = 80.0


class RiskManager:
    """Pre-trade gatekeeper and intraday risk controls."""

    def __init__(self, kite: KiteConnect, capital: float = settings.TOTAL_CAPITAL):
        self.kite = kite
        self.starting_capital = capital
        self.trades_today = 0
        self.consecutive_losses = 0
        self._paused_until: datetime | None = None
        # Track per-instrument ATR for volatility filtering
        self._atr_history: dict[str, list[float]] = {}

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
        candle_df: pd.DataFrame | None = None,
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

        # 8 — Total exposure cap
        qty = self.calculate_quantity(
            self.starting_capital, settings.RISK_PER_TRADE_PCT,
            entry_price, sl_price,
        )
        current_exposure = sum(
            p.entry_price * p.quantity for p in position_tracker.positions.values()
        )
        new_exposure = current_exposure + (entry_price * qty)
        max_exposure = self.starting_capital * MAX_TOTAL_EXPOSURE_PCT / 100
        if new_exposure > max_exposure:
            return False, (
                f"Total exposure would exceed {MAX_TOTAL_EXPOSURE_PCT}% cap "
                f"({new_exposure:.0f} > {max_exposure:.0f})."
            )

        # 9 — Volatility filter (skip if ATR > 2x 20-period average)
        if candle_df is not None and not candle_df.empty:
            blocked, reason = self._volatility_filter(symbol, candle_df)
            if blocked:
                return False, reason

        # 10 — Minimum SL distance sanity check
        sl_distance_pct = abs(entry_price - sl_price) / entry_price * 100
        if sl_distance_pct < 0.05:
            return False, f"SL too tight ({sl_distance_pct:.3f}%) — likely bad data."
        if sl_distance_pct > 5.0:
            return False, f"SL too wide ({sl_distance_pct:.1f}%) — excessive risk."

        return True, "OK"

    # ── Volatility filter ────────────────────────────────────────────
    def _volatility_filter(
        self, symbol: str, df: pd.DataFrame
    ) -> tuple[bool, str]:
        """
        Block trade if current ATR is > 2x the 20-period average ATR.
        Returns (blocked: bool, reason: str).
        """
        if len(df) < 21:
            return False, ""

        tr1 = df["high"] - df["low"]
        tr2 = (df["high"] - df["close"].shift()).abs()
        tr3 = (df["low"] - df["close"].shift()).abs()
        tr = pd.concat([tr1, tr2, tr3], axis=1).max(axis=1)
        atr_series = tr.rolling(14).mean()

        current_atr = atr_series.iloc[-1]
        avg_atr = atr_series.iloc[-21:-1].mean()

        if pd.isna(current_atr) or pd.isna(avg_atr) or avg_atr == 0:
            return False, ""

        if current_atr > 2.0 * avg_atr:
            reason = (
                f"Volatility too high for {symbol}: "
                f"ATR={current_atr:.2f} > 2x avg ATR={avg_atr:.2f}"
            )
            log.warning(reason)
            return True, reason

        return False, ""

    # ── Margin helper ────────────────────────────────────────────────
    @retry(max_retries=2, exceptions=(Exception,))
    def _has_sufficient_margin(self, entry_price: float, sl_price: float) -> bool:
        qty = self.calculate_quantity(
            self.starting_capital,
            settings.RISK_PER_TRADE_PCT,
            entry_price,
            sl_price,
        )
        try:
            margins = self.kite.margins("equity")
            available = margins.get("available", {}).get("live_balance", 0)

            # Try Kite's order margin API for accurate per-stock margin
            try:
                order_params = [{
                    "exchange": settings.EXCHANGE,
                    "tradingsymbol": "",  # filled by caller context
                    "transaction_type": "BUY",
                    "variety": "regular",
                    "product": settings.PRODUCT_TYPE,
                    "order_type": settings.ORDER_TYPE,
                    "quantity": qty,
                    "price": entry_price,
                }]
                margin_info = self.kite.order_margins(order_params)
                if margin_info and len(margin_info) > 0:
                    required = margin_info[0].get("total", entry_price * qty * 0.25)
                    if available < required:
                        log.warning(
                            "Margin insufficient: available=%.0f, required=%.0f (Kite API)",
                            available, required,
                        )
                    return available >= required
            except Exception:
                pass  # Fallback to estimate below

            # Fallback: conservative 30% estimate (25% was too optimistic for some stocks)
            required = entry_price * qty * 0.30
            return available >= required
        except Exception:
            log.warning("Margin check failed — blocking trade for safety.")
            return False

    # ── Record trade outcome ─────────────────────────────────────────
    def record_trade(self, pnl: float) -> None:
        self.trades_today += 1
        if pnl < 0:
            self.consecutive_losses += 1
            if self.consecutive_losses >= settings.CONSECUTIVE_LOSS_PAUSE_THRESHOLD:
                pause_until = now_ist().replace(second=0, microsecond=0)
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
