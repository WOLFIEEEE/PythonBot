"""
Main orchestrator — authentication, data feed, strategy evaluation,
order lifecycle, risk management, and scheduled square-off.

Hardened with: market-day check, thread safety, emergency exits,
external close detection, stale data skipping.
"""

from __future__ import annotations

import signal
import sys
import threading
import time
from datetime import datetime

import pandas as pd
from apscheduler.schedulers.background import BackgroundScheduler

from config import settings
from core.auth import authenticate
from core.data_feed import DataFeed
from core.order_manager import OrderManager
from core.position_tracker import PositionTracker
from core.risk_manager import RiskManager
from core.strategy import StrategyEngine
from strategies.ema_crossover import EMACrossoverStrategy
from strategies.orb import ORBStrategy
from strategies.supertrend import SupertrendStrategy
from strategies.vwap_breakout import VWAPBreakoutStrategy
from utils import db
from utils.helpers import (
    build_instrument_map,
    is_market_day,
    is_market_open,
    is_past_square_off_time,
    now_ist,
    resolve_tokens,
)
from utils.logger import get_logger
from utils.notifier import (
    notify_bot_status,
    notify_daily_summary,
    notify_order_error,
    notify_risk_breach,
    notify_trade_entry,
    notify_trade_exit,
)

log = get_logger("main")

# ── Globals ──────────────────────────────────────────────────────────
kite = None
order_mgr: OrderManager | None = None
risk_mgr: RiskManager | None = None
pos_tracker: PositionTracker | None = None
data_feed: DataFeed | None = None
strategy_engine: StrategyEngine | None = None
scheduler: BackgroundScheduler | None = None
shutdown_event = threading.Event()

# Lock to prevent concurrent signal processing for the same instrument
_signal_lock = threading.Lock()


# ── Strategy callback (fired on each new candle) ─────────────────────
def on_new_candle(symbol: str, df: pd.DataFrame) -> None:
    """Evaluate strategy and place trades if appropriate."""
    if shutdown_event.is_set():
        return
    if not is_market_open():
        return
    if is_past_square_off_time():
        return

    # Skip if data is stale (no recent ticks)
    if data_feed and data_feed.is_data_stale(symbol):
        log.warning("Stale data for %s — skipping signal evaluation.", symbol)
        return

    with _signal_lock:
        try:
            _process_signal(symbol, df)
            _manage_open_positions(symbol)
        except Exception:
            log.exception("Error processing candle for %s", symbol)


def _process_signal(symbol: str, df: pd.DataFrame) -> None:
    signal_val = strategy_engine.evaluate(symbol, df)
    if signal_val == "HOLD":
        return

    # Position reversal: if we have an opposite position, close it first
    existing_pos = pos_tracker.get_position(symbol)
    if existing_pos is not None:
        if existing_pos.direction == signal_val:
            # Same direction — already in position, skip
            return
        # Opposite signal — close existing position before entering new one
        log.info(
            "Signal reversal for %s: %s → %s — closing existing position.",
            symbol, existing_pos.direction, signal_val,
        )
        exit_dir = "SELL" if existing_pos.direction == "BUY" else "BUY"
        # Cancel pending SL/target orders
        if existing_pos.sl_order_id:
            order_mgr.cancel_order(existing_pos.sl_order_id)
        if existing_pos.target_order_id:
            order_mgr.cancel_order(existing_pos.target_order_id)
        # Market close
        close_oid = order_mgr.square_off(symbol, existing_pos.quantity, exit_dir)
        if close_oid:
            close_status = order_mgr.wait_for_fill(close_oid, timeout=15)
            exit_price = order_mgr.get_fill_price(close_oid) if close_status == "COMPLETE" else None
            exit_price = exit_price or (data_feed.get_ltp(symbol) if data_feed else None) or existing_pos.entry_price
            trade = pos_tracker.close_position(symbol, exit_price, "SIGNAL_REVERSAL")
            if trade:
                risk_mgr.record_trade(trade["net_pnl"])
                _log_and_notify_exit(trade)
                _persist_trade(trade)

    raw_price = df.iloc[-1]["close"]

    # Validate price sanity
    if raw_price <= 0 or pd.isna(raw_price):
        log.warning("Invalid entry price for %s: %s — skipping.", symbol, raw_price)
        return

    # Apply expected slippage to entry price for conservative risk calculation.
    # Market orders typically slip — use worse-case estimate for sizing.
    slippage = settings.SLIPPAGE_PCT / 100
    if signal_val == "BUY":
        entry_price = round(raw_price * (1 + slippage), 2)  # expect to buy higher
        sl_price = round(entry_price * (1 - settings.SL_PCT / 100), 2)
        target_price = round(entry_price * (1 + settings.TARGET_PCT / 100), 2)
    else:
        entry_price = round(raw_price * (1 - slippage), 2)  # expect to sell lower
        sl_price = round(entry_price * (1 + settings.SL_PCT / 100), 2)
        target_price = round(entry_price * (1 - settings.TARGET_PCT / 100), 2)

    # Pre-trade checks (now includes volatility filter with candle data)
    ok, reason = risk_mgr.pre_trade_checks(
        symbol, pos_tracker, entry_price, sl_price, candle_df=df
    )
    if not ok:
        log.info("Trade blocked for %s: %s", symbol, reason)
        return

    # Atomically reserve a position slot before placing the order.
    # This prevents two instruments from both passing MAX_OPEN_POSITIONS
    # check simultaneously and exceeding the limit.
    if not pos_tracker.reserve_slot(symbol):
        log.info("Slot reservation failed for %s (max positions or duplicate).", symbol)
        return

    qty = risk_mgr.calculate_quantity(
        settings.TOTAL_CAPITAL,
        settings.RISK_PER_TRADE_PCT,
        entry_price,
        sl_price,
    )

    # Place entry order
    entry_oid = order_mgr.place_entry_order(symbol, signal_val, qty)
    if not entry_oid:
        pos_tracker.release_slot(symbol)
        return

    # Wait for fill
    status = order_mgr.wait_for_fill(entry_oid, timeout=30)
    if status != "COMPLETE":
        log.warning("Entry order %s not filled (status=%s).", entry_oid, status)
        if status != "REJECTED":
            order_mgr.cancel_order(entry_oid)
        pos_tracker.release_slot(symbol)
        return

    fill_price = order_mgr.get_fill_price(entry_oid) or entry_price

    # Recalculate SL/target based on actual fill
    if signal_val == "BUY":
        sl_price = round(fill_price * (1 - settings.SL_PCT / 100), 2)
        target_price = round(fill_price * (1 + settings.TARGET_PCT / 100), 2)
        exit_dir = "SELL"
    else:
        sl_price = round(fill_price * (1 + settings.SL_PCT / 100), 2)
        target_price = round(fill_price * (1 - settings.TARGET_PCT / 100), 2)
        exit_dir = "BUY"

    # Place SL order — CRITICAL: if this fails, emergency exit
    sl_oid = order_mgr.place_sl_order(symbol, exit_dir, qty, sl_price)
    if sl_oid is None:
        log.error("SL placement failed for %s — initiating emergency exit.", symbol)
        order_mgr.emergency_exit(symbol, exit_dir, qty)
        return

    # Place target order (non-critical — position is still protected by SL)
    tgt_oid = order_mgr.place_target_order(symbol, exit_dir, qty, target_price)

    # Determine winning strategy from confluence engine
    sig_info = strategy_engine.last_signals.get(symbol, {})
    winning_strategy = sig_info.get("strategy", "Confluence")

    # Track the position
    pos_tracker.open_position(
        symbol=symbol,
        direction=signal_val,
        entry_price=fill_price,
        quantity=qty,
        sl_price=sl_price,
        target_price=target_price,
        strategy=winning_strategy,
        entry_order_id=entry_oid,
        sl_order_id=sl_oid,
        target_order_id=tgt_oid,
    )

    risk_amount = abs(fill_price - sl_price) * qty
    notify_trade_entry(
        symbol, signal_val, fill_price, qty, sl_price, target_price,
        winning_strategy, risk_amount,
    )


def _manage_open_positions(symbol: str) -> None:
    """Check SL/target fills, update trailing SL for an open position."""
    pos = pos_tracker.get_position(symbol)
    if pos is None:
        return

    # Check SL order
    if pos.sl_order_id:
        sl_status = order_mgr.get_order_status(pos.sl_order_id)
        if sl_status == "COMPLETE":
            exit_price = order_mgr.get_fill_price(pos.sl_order_id) or pos.sl_price
            if pos.target_order_id:
                order_mgr.cancel_order(pos.target_order_id)
            trade = pos_tracker.close_position(symbol, exit_price, "SL_HIT")
            if trade:
                risk_mgr.record_trade(trade["net_pnl"])
                _log_and_notify_exit(trade)
                _persist_trade(trade)
            return

    # Check target order
    if pos.target_order_id:
        tgt_status = order_mgr.get_order_status(pos.target_order_id)
        if tgt_status == "COMPLETE":
            exit_price = order_mgr.get_fill_price(pos.target_order_id) or pos.target_price
            if pos.sl_order_id:
                order_mgr.cancel_order(pos.sl_order_id)
            trade = pos_tracker.close_position(symbol, exit_price, "TARGET_HIT")
            if trade:
                risk_mgr.record_trade(trade["net_pnl"])
                _log_and_notify_exit(trade)
                _persist_trade(trade)
            return

    # Trailing SL — atomic: revert tracker if exchange modify fails
    if settings.TRAILING_SL and data_feed:
        ltp = data_feed.get_ltp(symbol)
        if ltp:
            old_sl = pos.trailing_sl
            new_sl = pos_tracker.update_trailing_sl(symbol, ltp)
            if new_sl and pos.sl_order_id:
                exit_dir = "SELL" if pos.direction == "BUY" else "BUY"
                success = order_mgr.modify_sl_order(
                    pos.sl_order_id, new_sl, direction=exit_dir,
                )
                if not success:
                    # Revert: exchange still has the old trigger
                    pos_tracker.revert_trailing_sl(symbol, old_sl)
                    log.warning(
                        "Trailing SL modify failed for %s — reverted to %.2f",
                        symbol, old_sl,
                    )


def _log_and_notify_exit(trade: dict) -> None:
    notify_trade_exit(
        trade["instrument"],
        trade["exit_price"],
        trade["net_pnl"],
        trade["exit_reason"],
        trade["duration_min"],
    )


def _persist_trade(trade: dict) -> None:
    order_ids = [
        trade.get("entry_order_id", ""),
        trade.get("sl_order_id", ""),
        trade.get("target_order_id", ""),
    ]
    db.log_trade(
        instrument=trade["instrument"],
        direction=trade["direction"],
        entry_price=trade["entry_price"],
        exit_price=trade["exit_price"],
        quantity=trade["quantity"],
        entry_time=trade["entry_time"],
        exit_time=trade["exit_time"],
        pnl=trade["net_pnl"],
        strategy=trade["strategy"],
        sl_price=trade["sl_price"],
        target_price=trade["target_price"],
        exit_reason=trade["exit_reason"],
        order_ids=order_ids,
        charges=trade["charges"],
    )


# ── Periodic tasks ───────────────────────────────────────────────────
def _heartbeat() -> None:
    """Verify Kite session is alive."""
    try:
        kite.profile()
        log.debug("Heartbeat OK.")
    except Exception as exc:
        log.error("Heartbeat failed: %s", exc)
        notify_order_error("SYSTEM", f"Heartbeat failed: {exc}")


def _sync_positions() -> None:
    """Reconcile positions with Kite and handle external closes."""
    if not pos_tracker:
        return

    # Build live prices from data feed
    live_prices: dict[str, float] = {}
    if data_feed:
        for sym in pos_tracker.get_open_symbols():
            ltp = data_feed.get_ltp(sym)
            if ltp:
                live_prices[sym] = ltp
    pos_tracker.update_unrealized_pnl(live_prices)

    # Detect externally closed positions
    externally_closed = pos_tracker.sync_with_kite()
    for sym in externally_closed:
        ltp = live_prices.get(sym)
        pos = pos_tracker.get_position(sym)
        if pos and ltp:
            # Cancel any pending SL/target orders
            if pos.sl_order_id:
                order_mgr.cancel_order(pos.sl_order_id)
            if pos.target_order_id:
                order_mgr.cancel_order(pos.target_order_id)
            trade = pos_tracker.close_position(sym, ltp, "EXTERNAL_CLOSE")
            if trade:
                risk_mgr.record_trade(trade["net_pnl"])
                _log_and_notify_exit(trade)
                _persist_trade(trade)

    # Check daily loss circuit breaker
    if risk_mgr and risk_mgr.should_stop_trading(pos_tracker):
        log.warning("DAILY LOSS LIMIT HIT — squaring off everything.")
        notify_risk_breach("Daily loss limit hit — squaring off all positions.")
        _square_off_all()


def _square_off_all() -> None:
    """Square off all open positions and cancel pending orders."""
    log.info("Initiating square-off of all positions.")
    if order_mgr:
        order_mgr.cancel_all_pending()
        order_mgr.square_off_all()

    # Close positions in tracker
    if pos_tracker and data_feed:
        for sym in pos_tracker.get_open_symbols():
            ltp = data_feed.get_ltp(sym)
            pos = pos_tracker.get_position(sym)
            if not pos:
                continue
            exit_price = ltp or pos.entry_price
            trade = pos_tracker.close_position(sym, exit_price, "SQUARE_OFF")
            if trade:
                risk_mgr.record_trade(trade["net_pnl"])
                _log_and_notify_exit(trade)
                _persist_trade(trade)


def _emergency_square_off_check() -> None:
    """
    Secondary safety net: if any positions remain open after primary square-off
    at SQUARE_OFF_TIME, force-close them before Zerodha's auto-square at 3:20 PM.
    Zerodha auto-square-off at 3:20-3:25 uses market orders at worst available price.
    """
    if not pos_tracker:
        return
    open_positions = pos_tracker.get_open_symbols()
    if open_positions:
        log.warning(
            "EMERGENCY SQUARE-OFF: %d positions still open at %s — "
            "force-closing before Zerodha auto-square-off at 15:20.",
            len(open_positions),
            settings.EMERGENCY_SQUARE_OFF_TIME,
        )
        notify_risk_breach(
            f"Emergency square-off: {len(open_positions)} positions still open "
            f"at {settings.EMERGENCY_SQUARE_OFF_TIME}"
        )
        _square_off_all()


def _daily_summary() -> None:
    """Log and send the daily summary."""
    if not pos_tracker:
        return

    s = pos_tracker.daily_summary
    db.save_daily_summary(
        total=s["total_trades"],
        wins=s["winning_trades"],
        losses=s["losing_trades"],
        gross_pnl=s["gross_pnl"],
        net_pnl=s["net_pnl"],
        max_dd=s["max_drawdown"],
        capital_used=s["capital_used"],
    )
    notify_daily_summary(
        now_ist().strftime("%d-%b-%Y"),
        s["total_trades"],
        s["winning_trades"],
        s["losing_trades"],
        s["gross_pnl"],
        s["net_pnl"],
        s["max_drawdown"],
    )
    log.info("Daily summary: %s", s)


# ── Shutdown ─────────────────────────────────────────────────────────
def _graceful_shutdown(signum=None, frame=None) -> None:
    if shutdown_event.is_set():
        return  # Prevent double shutdown
    log.info("Shutdown signal received — cleaning up...")
    shutdown_event.set()

    _square_off_all()
    _daily_summary()

    if data_feed:
        data_feed.stop()
    if scheduler:
        scheduler.shutdown(wait=False)

    notify_bot_status("STOPPED")
    log.info("Bot stopped.")
    sys.exit(0)


# ── Main ─────────────────────────────────────────────────────────────
def main() -> None:
    global kite, order_mgr, risk_mgr, pos_tracker, data_feed
    global strategy_engine, scheduler

    # 0. Market day check
    if not is_market_day():
        log.info("Today is not a trading day (weekend). Exiting.")
        print("Today is not a trading day (weekend). Exiting.")
        return

    # 1. Init database
    db.init_db()

    # 2. Authenticate
    log.info("Authenticating with Kite Connect...")
    kite = authenticate()

    # 3. Fetch instruments & resolve tokens
    log.info("Fetching instrument list...")
    instrument_map = build_instrument_map(kite)
    token_map = resolve_tokens(instrument_map, settings.WATCHLIST)
    if not token_map:
        log.error("No instruments resolved — exiting.")
        sys.exit(1)
    log.info("Resolved %d instruments.", len(token_map))

    # Invert map for data feed: {token: symbol}
    inv_map = {v: k for k, v in token_map.items()}

    # 4. Initialize components
    order_mgr = OrderManager(kite)
    risk_mgr = RiskManager(kite, capital=settings.TOTAL_CAPITAL)
    pos_tracker = PositionTracker(kite)

    strategy_engine = StrategyEngine(
        strategy_classes=[
            EMACrossoverStrategy,
            SupertrendStrategy,
            VWAPBreakoutStrategy,
            ORBStrategy,
        ],
        min_agreement=2,
    )

    # 5. Start WebSocket data feed
    data_feed = DataFeed(
        access_token=kite.access_token,
        token_symbol_map=inv_map,
        on_candle=on_new_candle,
        candle_interval=settings.CANDLE_INTERVAL_MINUTES,
    )
    data_feed.start()

    # 6. Schedule periodic tasks
    scheduler = BackgroundScheduler(timezone="Asia/Kolkata")

    # Heartbeat every 5 minutes
    scheduler.add_job(
        _heartbeat,
        "interval",
        seconds=settings.HEARTBEAT_INTERVAL,
        id="heartbeat",
    )

    # Position sync every 30 seconds
    scheduler.add_job(
        _sync_positions,
        "interval",
        seconds=settings.POSITION_SYNC_INTERVAL,
        id="position_sync",
    )

    # Square-off at SQUARE_OFF_TIME (primary)
    sq_h, sq_m = map(int, settings.SQUARE_OFF_TIME.split(":"))
    scheduler.add_job(
        _square_off_all,
        "cron",
        hour=sq_h,
        minute=sq_m,
        id="square_off",
    )

    # Emergency square-off: secondary safety net before Zerodha auto-squares at 3:20-3:25
    esq_h, esq_m = map(int, settings.EMERGENCY_SQUARE_OFF_TIME.split(":"))
    scheduler.add_job(
        _emergency_square_off_check,
        "cron",
        hour=esq_h,
        minute=esq_m,
        id="emergency_square_off",
    )

    # Daily summary at MARKET_CLOSE
    mc_h, mc_m = map(int, settings.MARKET_CLOSE.split(":"))
    scheduler.add_job(
        _daily_summary,
        "cron",
        hour=mc_h,
        minute=mc_m,
        id="daily_summary",
    )

    scheduler.start()

    # 7. Register graceful shutdown handlers
    signal.signal(signal.SIGINT, _graceful_shutdown)
    signal.signal(signal.SIGTERM, _graceful_shutdown)

    notify_bot_status("STARTED")
    log.info(
        "Bot running — watching %d instruments. Square-off at %s.",
        len(token_map),
        settings.SQUARE_OFF_TIME,
    )

    # 8. Keep main thread alive
    try:
        while not shutdown_event.is_set():
            shutdown_event.wait(timeout=1)
    except KeyboardInterrupt:
        _graceful_shutdown()


if __name__ == "__main__":
    main()
