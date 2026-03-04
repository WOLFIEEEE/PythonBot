"""
Bot runner — bridges the GUI launcher to the trading engine.

Provides:
  - run_bot(params, log_queue): start the full bot lifecycle
  - stop_bot(): graceful shutdown
  - emergency_square_off(): immediate close-all
  - get_bot_state() / get_open_positions() / get_closed_trades(): read-only
    accessors for the dashboard.
"""

from __future__ import annotations

import json
import logging
import os
import queue
import signal
import sys
import threading
from datetime import datetime
from pathlib import Path
from typing import Any

# Ensure project root is on path
PROJECT_ROOT = str(Path(__file__).resolve().parent.parent)
if PROJECT_ROOT not in sys.path:
    sys.path.insert(0, PROJECT_ROOT)

# ── Module-level references (set by run_bot) ─────────────────────────
_kite = None
_order_mgr = None
_risk_mgr = None
_pos_tracker = None
_data_feed = None
_strategy_engine = None
_scheduler = None
_shutdown_event = threading.Event()
_log_queue: queue.Queue[str] | None = None


# ═══════════════════════════════════════════════════════════════════════
#  Custom log handler that routes to the GUI queue
# ═══════════════════════════════════════════════════════════════════════
class QueueLogHandler(logging.Handler):
    def __init__(self, q: queue.Queue[str]):
        super().__init__()
        self._q = q
        self.setFormatter(logging.Formatter(
            "%(asctime)s | %(name)-20s | %(levelname)-8s | %(message)s",
            datefmt="%H:%M:%S",
        ))

    def emit(self, record: logging.LogRecord) -> None:
        try:
            self._q.put(self.format(record))
        except Exception:
            pass


def _install_queue_logger(q: queue.Queue[str]) -> None:
    """Attach QueueLogHandler to the root logger so all bot logs go to GUI."""
    handler = QueueLogHandler(q)
    handler.setLevel(logging.DEBUG)
    root = logging.getLogger()
    root.addHandler(handler)


# ═══════════════════════════════════════════════════════════════════════
#  Apply settings from GUI params to config.settings at runtime
# ═══════════════════════════════════════════════════════════════════════
def _apply_params(params: dict[str, Any]) -> None:
    """Override config.settings values from the GUI-collected params."""
    from config import settings

    settings.KITE_API_KEY = params["api_key"]
    settings.KITE_API_SECRET = params["api_secret"]
    settings.TOTAL_CAPITAL = float(params.get("capital", 100_000))
    settings.RISK_PER_TRADE_PCT = float(params.get("risk_pct", 1.0))
    settings.MAX_TRADES_PER_DAY = int(params.get("max_trades", 5))
    settings.MAX_DAILY_LOSS_PCT = float(params.get("max_loss_pct", 3.0))
    settings.MAX_OPEN_POSITIONS = int(params.get("max_positions", 3))
    settings.SL_PCT = float(params.get("sl_pct", 0.5))
    settings.TARGET_PCT = float(params.get("target_pct", 1.0))
    settings.TRAILING_SL = bool(params.get("trailing_sl", True))
    settings.TRAILING_SL_PCT = float(params.get("trailing_sl_pct", 0.3))
    settings.CANDLE_INTERVAL_MINUTES = int(params.get("candle_interval", 5))
    settings.SQUARE_OFF_TIME = params.get("square_off_time", "15:10")
    settings.NO_NEW_TRADES_AFTER = params.get("no_new_after", "14:30")
    settings.WATCHLIST = params.get("watchlist", settings.WATCHLIST)

    if params.get("tg_token"):
        settings.TELEGRAM_BOT_TOKEN = params["tg_token"]
    if params.get("tg_chat_id"):
        settings.TELEGRAM_CHAT_ID = params["tg_chat_id"]


# ═══════════════════════════════════════════════════════════════════════
#  Strategy class resolver
# ═══════════════════════════════════════════════════════════════════════
def _resolve_strategies(keys: list[str]) -> list:
    from strategies.ema_crossover import EMACrossoverStrategy
    from strategies.orb import ORBStrategy
    from strategies.supertrend import SupertrendStrategy
    from strategies.vwap_breakout import VWAPBreakoutStrategy

    mapping = {
        "ema_crossover": EMACrossoverStrategy,
        "supertrend": SupertrendStrategy,
        "vwap_breakout": VWAPBreakoutStrategy,
        "orb": ORBStrategy,
    }
    return [mapping[k] for k in keys if k in mapping]


# ═══════════════════════════════════════════════════════════════════════
#  The on_candle callback (identical logic to main.py but using module refs)
# ═══════════════════════════════════════════════════════════════════════
_signal_lock = threading.Lock()


def _on_new_candle(symbol: str, df) -> None:
    import pandas as pd

    from config import settings
    from utils.helpers import is_market_open, is_past_square_off_time

    if _shutdown_event.is_set():
        return
    if not is_market_open():
        return
    if is_past_square_off_time():
        return
    if _data_feed and _data_feed.is_data_stale(symbol):
        return

    with _signal_lock:
        try:
            _process_signal(symbol, df)
            _manage_position(symbol)
        except Exception as exc:
            _log(f"Error processing {symbol}: {exc}", level="ERROR")


def _process_signal(symbol: str, df) -> None:
    import pandas as pd

    from config import settings
    from utils import db
    from utils.notifier import notify_trade_entry

    signal_val = _strategy_engine.evaluate(symbol, df)
    if signal_val == "HOLD":
        return

    entry_price = df.iloc[-1]["close"]
    if entry_price <= 0 or pd.isna(entry_price):
        return

    if signal_val == "BUY":
        sl_price = round(entry_price * (1 - settings.SL_PCT / 100), 2)
        target_price = round(entry_price * (1 + settings.TARGET_PCT / 100), 2)
    else:
        sl_price = round(entry_price * (1 + settings.SL_PCT / 100), 2)
        target_price = round(entry_price * (1 - settings.TARGET_PCT / 100), 2)

    ok, reason = _risk_mgr.pre_trade_checks(
        symbol, _pos_tracker, entry_price, sl_price, candle_df=df
    )
    if not ok:
        _log(f"Trade blocked {symbol}: {reason}")
        return

    qty = _risk_mgr.calculate_quantity(
        settings.TOTAL_CAPITAL, settings.RISK_PER_TRADE_PCT, entry_price, sl_price
    )

    entry_oid = _order_mgr.place_entry_order(symbol, signal_val, qty)
    if not entry_oid:
        return

    status = _order_mgr.wait_for_fill(entry_oid, timeout=30)
    if status != "COMPLETE":
        if status != "REJECTED":
            _order_mgr.cancel_order(entry_oid)
        return

    fill_price = _order_mgr.get_fill_price(entry_oid) or entry_price
    if signal_val == "BUY":
        sl_price = round(fill_price * (1 - settings.SL_PCT / 100), 2)
        target_price = round(fill_price * (1 + settings.TARGET_PCT / 100), 2)
        exit_dir = "SELL"
    else:
        sl_price = round(fill_price * (1 + settings.SL_PCT / 100), 2)
        target_price = round(fill_price * (1 - settings.TARGET_PCT / 100), 2)
        exit_dir = "BUY"

    sl_oid = _order_mgr.place_sl_order(symbol, exit_dir, qty, sl_price)
    if sl_oid is None:
        _order_mgr.emergency_exit(symbol, exit_dir, qty)
        return

    tgt_oid = _order_mgr.place_target_order(symbol, exit_dir, qty, target_price)

    strategy_name = _strategy_engine.last_signals.get(symbol, {}).get("strategy", "Confluence")
    _pos_tracker.open_position(
        symbol=symbol, direction=signal_val, entry_price=fill_price,
        quantity=qty, sl_price=sl_price, target_price=target_price,
        strategy=strategy_name, entry_order_id=entry_oid,
        sl_order_id=sl_oid, target_order_id=tgt_oid,
    )

    risk_amount = abs(fill_price - sl_price) * qty
    notify_trade_entry(symbol, signal_val, fill_price, qty, sl_price,
                       target_price, strategy_name, risk_amount)


def _manage_position(symbol: str) -> None:
    from config import settings
    from utils import db
    from utils.notifier import notify_trade_exit

    pos = _pos_tracker.get_position(symbol)
    if pos is None:
        return

    # SL hit?
    if pos.sl_order_id:
        sl_st = _order_mgr.get_order_status(pos.sl_order_id)
        if sl_st == "COMPLETE":
            exit_px = _order_mgr.get_fill_price(pos.sl_order_id) or pos.sl_price
            if pos.target_order_id:
                _order_mgr.cancel_order(pos.target_order_id)
            trade = _pos_tracker.close_position(symbol, exit_px, "SL_HIT")
            if trade:
                _risk_mgr.record_trade(trade["net_pnl"])
                _persist_trade(trade)
                notify_trade_exit(trade["instrument"], trade["exit_price"],
                                  trade["net_pnl"], "SL_HIT", trade["duration_min"])
            return

    # Target hit?
    if pos.target_order_id:
        tgt_st = _order_mgr.get_order_status(pos.target_order_id)
        if tgt_st == "COMPLETE":
            exit_px = _order_mgr.get_fill_price(pos.target_order_id) or pos.target_price
            if pos.sl_order_id:
                _order_mgr.cancel_order(pos.sl_order_id)
            trade = _pos_tracker.close_position(symbol, exit_px, "TARGET_HIT")
            if trade:
                _risk_mgr.record_trade(trade["net_pnl"])
                _persist_trade(trade)
                notify_trade_exit(trade["instrument"], trade["exit_price"],
                                  trade["net_pnl"], "TARGET_HIT", trade["duration_min"])
            return

    # Trailing SL
    if settings.TRAILING_SL and _data_feed:
        ltp = _data_feed.get_ltp(symbol)
        if ltp:
            new_sl = _pos_tracker.update_trailing_sl(symbol, ltp)
            if new_sl and pos.sl_order_id:
                _order_mgr.modify_sl_order(pos.sl_order_id, new_sl)


def _persist_trade(trade: dict) -> None:
    from utils import db
    order_ids = [
        trade.get("entry_order_id", ""),
        trade.get("sl_order_id", ""),
        trade.get("target_order_id", ""),
    ]
    db.log_trade(
        instrument=trade["instrument"], direction=trade["direction"],
        entry_price=trade["entry_price"], exit_price=trade["exit_price"],
        quantity=trade["quantity"], entry_time=trade["entry_time"],
        exit_time=trade["exit_time"], pnl=trade["net_pnl"],
        strategy=trade["strategy"], sl_price=trade["sl_price"],
        target_price=trade["target_price"], exit_reason=trade["exit_reason"],
        order_ids=order_ids, charges=trade["charges"],
    )


# ═══════════════════════════════════════════════════════════════════════
#  Scheduled tasks
# ═══════════════════════════════════════════════════════════════════════
def _heartbeat() -> None:
    try:
        _kite.profile()
    except Exception as exc:
        _log(f"Heartbeat failed: {exc}", level="ERROR")


def _sync_positions() -> None:
    if not _pos_tracker or not _data_feed:
        return

    live_prices: dict[str, float] = {}
    for sym in _pos_tracker.get_open_symbols():
        ltp = _data_feed.get_ltp(sym)
        if ltp:
            live_prices[sym] = ltp
    _pos_tracker.update_unrealized_pnl(live_prices)

    # External closes
    externally_closed = _pos_tracker.sync_with_kite()
    for sym in externally_closed:
        ltp = live_prices.get(sym)
        pos = _pos_tracker.get_position(sym)
        if pos and ltp:
            if pos.sl_order_id:
                _order_mgr.cancel_order(pos.sl_order_id)
            if pos.target_order_id:
                _order_mgr.cancel_order(pos.target_order_id)
            trade = _pos_tracker.close_position(sym, ltp, "EXTERNAL_CLOSE")
            if trade:
                _risk_mgr.record_trade(trade["net_pnl"])
                _persist_trade(trade)

    # Circuit breaker
    if _risk_mgr and _risk_mgr.should_stop_trading(_pos_tracker):
        _log("DAILY LOSS LIMIT HIT — squaring off.", level="WARNING")
        _square_off_all()


def _square_off_all() -> None:
    _log("Initiating square-off of all positions.")
    if _order_mgr:
        _order_mgr.cancel_all_pending()
        _order_mgr.square_off_all()

    if _pos_tracker and _data_feed:
        for sym in _pos_tracker.get_open_symbols():
            ltp = _data_feed.get_ltp(sym)
            pos = _pos_tracker.get_position(sym)
            if not pos:
                continue
            exit_price = ltp or pos.entry_price
            trade = _pos_tracker.close_position(sym, exit_price, "SQUARE_OFF")
            if trade:
                _risk_mgr.record_trade(trade["net_pnl"])
                _persist_trade(trade)


def _daily_summary() -> None:
    if not _pos_tracker:
        return
    from utils import db
    from utils.helpers import now_ist
    from utils.notifier import notify_daily_summary

    s = _pos_tracker.daily_summary
    db.save_daily_summary(
        total=s["total_trades"], wins=s["winning_trades"],
        losses=s["losing_trades"], gross_pnl=s["gross_pnl"],
        net_pnl=s["net_pnl"], max_dd=s["max_drawdown"],
        capital_used=s["capital_used"],
    )
    notify_daily_summary(
        now_ist().strftime("%d-%b-%Y"),
        s["total_trades"], s["winning_trades"], s["losing_trades"],
        s["gross_pnl"], s["net_pnl"], s["max_drawdown"],
    )


def _log(msg: str, level: str = "INFO") -> None:
    if _log_queue:
        ts = datetime.now().strftime("%H:%M:%S")
        _log_queue.put(f"{ts} | bot_runner          | {level:8s} | {msg}")


# ═══════════════════════════════════════════════════════════════════════
#  Public API — called by GUI
# ═══════════════════════════════════════════════════════════════════════
def run_bot(params: dict[str, Any], log_q: queue.Queue[str]) -> None:
    """Main entry — run the full bot lifecycle. Blocks until shutdown."""
    global _kite, _order_mgr, _risk_mgr, _pos_tracker, _data_feed
    global _strategy_engine, _scheduler, _log_queue

    _log_queue = log_q
    _shutdown_event.clear()

    _install_queue_logger(log_q)
    _apply_params(params)

    from apscheduler.schedulers.background import BackgroundScheduler

    from config import settings
    from core.auth import authenticate
    from core.data_feed import DataFeed
    from core.order_manager import OrderManager
    from core.position_tracker import PositionTracker
    from core.risk_manager import RiskManager
    from core.strategy import StrategyEngine
    from utils import db
    from utils.helpers import build_instrument_map, resolve_tokens
    from utils.notifier import notify_bot_status

    # 1. Database
    db.init_db()
    _log("Database initialised.")

    # 2. Auth
    _log("Authenticating with Kite Connect...")
    _kite = authenticate()
    _log("Authenticated successfully.")

    # 3. Instruments
    _log("Fetching instruments...")
    inst_map = build_instrument_map(_kite)
    token_map = resolve_tokens(inst_map, settings.WATCHLIST)
    if not token_map:
        _log("No instruments resolved — aborting.", level="ERROR")
        return
    _log(f"Resolved {len(token_map)} instruments.")

    inv_map = {v: k for k, v in token_map.items()}

    # 4. Components
    _order_mgr = OrderManager(_kite)
    _risk_mgr = RiskManager(_kite, capital=settings.TOTAL_CAPITAL)
    _pos_tracker = PositionTracker(_kite)

    strat_classes = _resolve_strategies(params.get("strategies", []))
    if not strat_classes:
        _log("No valid strategies selected — aborting.", level="ERROR")
        return

    _strategy_engine = StrategyEngine(
        strategy_classes=strat_classes,
        min_agreement=params.get("min_confluence", 2),
    )
    _log(f"Strategy engine: {[c.name for c in strat_classes]}, min_agreement={params.get('min_confluence', 2)}")

    # 5. Data feed
    _data_feed = DataFeed(
        access_token=_kite.access_token,
        token_symbol_map=inv_map,
        on_candle=_on_new_candle,
        candle_interval=settings.CANDLE_INTERVAL_MINUTES,
    )
    _data_feed.start()
    _log("WebSocket data feed started.")

    # 6. Scheduler
    _scheduler = BackgroundScheduler(timezone="Asia/Kolkata")
    _scheduler.add_job(_heartbeat, "interval", seconds=settings.HEARTBEAT_INTERVAL, id="hb")
    _scheduler.add_job(_sync_positions, "interval", seconds=settings.POSITION_SYNC_INTERVAL, id="sync")

    sq_h, sq_m = map(int, settings.SQUARE_OFF_TIME.split(":"))
    _scheduler.add_job(_square_off_all, "cron", hour=sq_h, minute=sq_m, id="sqoff")

    mc_h, mc_m = map(int, settings.MARKET_CLOSE.split(":"))
    _scheduler.add_job(_daily_summary, "cron", hour=mc_h, minute=mc_m, id="summary")
    _scheduler.start()

    notify_bot_status("STARTED")
    _log(f"Bot running — {len(token_map)} instruments, square-off at {settings.SQUARE_OFF_TIME}.")

    # 7. Wait
    _shutdown_event.wait()

    # Cleanup
    _log("Shutting down...")
    _square_off_all()
    _daily_summary()
    if _data_feed:
        _data_feed.stop()
    if _scheduler:
        _scheduler.shutdown(wait=False)
    notify_bot_status("STOPPED")
    _log("Bot stopped.")


def stop_bot() -> None:
    """Signal the bot to shut down gracefully."""
    _shutdown_event.set()


def emergency_square_off() -> None:
    """Immediately close all positions."""
    _square_off_all()


# ── Read-only accessors for dashboard ─────────────────────────────────
def get_bot_state() -> dict[str, Any] | None:
    if not _pos_tracker:
        return None
    s = _pos_tracker.daily_summary
    return {
        "realized_pnl": _pos_tracker.total_realized_pnl,
        "unrealized_pnl": _pos_tracker.total_unrealized_pnl,
        "trades_today": s["total_trades"],
        "wins": s["winning_trades"],
        "open_positions": len(_pos_tracker.positions),
    }


def get_open_positions() -> list[dict[str, Any]]:
    if not _pos_tracker:
        return []
    result = []
    for sym in _pos_tracker.get_open_symbols():
        pos = _pos_tracker.get_position(sym)
        if pos:
            result.append({
                "instrument": pos.instrument,
                "direction": pos.direction,
                "entry_price": pos.entry_price,
                "quantity": pos.quantity,
                "sl_price": pos.sl_price,
                "target_price": pos.target_price,
                "pnl": pos.current_pnl,
                "strategy": pos.strategy,
            })
    return result


def get_closed_trades() -> list[dict[str, Any]]:
    if not _pos_tracker:
        return []
    with _pos_tracker._lock:
        return list(_pos_tracker.closed_trades)
