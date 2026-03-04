"""
Main orchestrator — authentication, data feed, strategy evaluation,
order lifecycle, risk management, and scheduled square-off.

Hardened with: market-day check, thread safety, emergency exits,
external close detection, stale data skipping, auto-evolution.
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
from core.adaptive_engine import AdaptiveEngine
from core.correlation import correlation_check
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
adaptive: AdaptiveEngine | None = None
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

    # Gap filter: check opening gap on first candle, skip early candles if gap is large
    if data_feed:
        candle_count = data_feed.aggregator.get_candle_count(symbol)
        if candle_count == 1 and len(df) > 0:
            open_price = df.iloc[0]["open"]
            data_feed.aggregator.check_opening_gap(symbol, open_price)

        if data_feed.aggregator.should_skip_for_gap(symbol):
            log.info("Skipping %s — gap filter active (early candles after large gap).", symbol)
            # Still manage existing positions even during gap skip
            with _signal_lock:
                _manage_open_positions(symbol)
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

    # Correlation check — prevent concentrated sector exposure and correlated positions
    open_symbols = pos_tracker.get_open_symbols()
    if open_symbols:
        candle_data = {}
        if data_feed:
            for sym in open_symbols:
                sym_df = data_feed.get_dataframe(sym)
                if sym_df is not None and not sym_df.empty:
                    candle_data[sym] = sym_df
        blocked, reason = correlation_check(symbol, df, open_symbols, candle_data)
        if blocked:
            log.info("Trade blocked by correlation filter for %s: %s", symbol, reason)
            return

    # Atomically reserve a position slot before placing the order.
    # This prevents two instruments from both passing MAX_OPEN_POSITIONS
    # check simultaneously and exceeding the limit.
    if not pos_tracker.reserve_slot(symbol):
        log.info("Slot reservation failed for %s (max positions or duplicate).", symbol)
        return

    total_qty = risk_mgr.calculate_quantity(
        settings.TOTAL_CAPITAL,
        settings.RISK_PER_TRADE_PCT,
        entry_price,
        sl_price,
    )

    # Regime-based adjustments from advanced regime detection
    sig_info = strategy_engine.last_signals.get(symbol, {})
    if sig_info.get("should_reduce_size"):
        # Reduce size by 40% on expiry days, open auction, or extreme volatility
        total_qty = max(1, int(total_qty * 0.6))
        log.info(
            "%s | Position size reduced to %d (regime: %s, special: %s)",
            symbol, total_qty,
            sig_info.get("micro_regime", "?"), sig_info.get("special_day", "?"),
        )

    if sig_info.get("should_widen_sl"):
        # Widen SL by 30% during volatile/expiry conditions
        sl_widen = 1.3
        if signal_val == "BUY":
            sl_price = round(entry_price * (1 - settings.SL_PCT * sl_widen / 100), 2)
        else:
            sl_price = round(entry_price * (1 + settings.SL_PCT * sl_widen / 100), 2)
        log.info("%s | SL widened to %.2f (volatile/expiry conditions)", symbol, sl_price)

    # Scaled entry: start with 50% of planned quantity.
    # Remaining 25% added on each of the next 2 candles if price holds.
    initial_qty = max(1, total_qty // 2)
    scale_in_remaining = total_qty - initial_qty

    # Place entry order (initial tranche)
    entry_oid = order_mgr.place_entry_order(symbol, signal_val, initial_qty)
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
    sl_oid = order_mgr.place_sl_order(symbol, exit_dir, initial_qty, sl_price)
    if sl_oid is None:
        log.error("SL placement failed for %s — initiating emergency exit.", symbol)
        order_mgr.emergency_exit(symbol, exit_dir, initial_qty)
        return

    # Place target order (non-critical — position is still protected by SL)
    tgt_oid = order_mgr.place_target_order(symbol, exit_dir, initial_qty, target_price)

    # Determine winning strategy from confluence engine
    sig_info = strategy_engine.last_signals.get(symbol, {})
    winning_strategy = sig_info.get("strategy", "Confluence")

    # Track the position (with scale-in metadata)
    pos = pos_tracker.open_position(
        symbol=symbol,
        direction=signal_val,
        entry_price=fill_price,
        quantity=initial_qty,
        sl_price=sl_price,
        target_price=target_price,
        strategy=winning_strategy,
        entry_order_id=entry_oid,
        sl_order_id=sl_oid,
        target_order_id=tgt_oid,
    )
    # Set scale-in tracking fields
    pos.total_planned_qty = total_qty
    pos.scale_in_remaining = scale_in_remaining
    pos.scale_in_candles_waited = 0
    pos.scale_in_complete = (scale_in_remaining == 0)

    risk_amount = abs(fill_price - sl_price) * initial_qty
    notify_trade_entry(
        symbol, signal_val, fill_price, initial_qty, sl_price, target_price,
        winning_strategy, risk_amount,
    )


def _manage_open_positions(symbol: str) -> None:
    """Check SL/target fills, scale-in add-ons, update trailing SL for an open position."""
    pos = pos_tracker.get_position(symbol)
    if pos is None:
        return

    # Scale-in: add 25% of planned quantity on each of the next 2 candles
    # Only if price is still moving in our favour (not reversing)
    if not pos.scale_in_complete and pos.scale_in_remaining > 0:
        candle_count = pos_tracker.increment_scale_candle(symbol)
        ltp = data_feed.get_ltp(symbol) if data_feed else None

        # Add on candle 1 and candle 2 after entry
        if candle_count in (1, 2) and ltp:
            price_ok = False
            if pos.direction == "BUY" and ltp > pos.entry_price:
                price_ok = True  # Price moving up — confirm BUY
            elif pos.direction == "SELL" and ltp < pos.entry_price:
                price_ok = True  # Price moving down — confirm SELL

            if price_ok:
                addon_qty = max(1, pos.total_planned_qty // 4)
                addon_qty = min(addon_qty, pos.scale_in_remaining)
                addon_oid = order_mgr.place_entry_order(symbol, pos.direction, addon_qty)
                if addon_oid:
                    fill_status = order_mgr.wait_for_fill(addon_oid, timeout=10)
                    if fill_status == "COMPLETE":
                        addon_fill = order_mgr.get_fill_price(addon_oid) or ltp
                        pos_tracker.add_to_position(symbol, addon_qty, addon_fill)
                        # Update SL/target orders for new total quantity
                        new_total_qty = pos.quantity  # already updated by add_to_position
                        exit_dir = "SELL" if pos.direction == "BUY" else "BUY"
                        if pos.sl_order_id:
                            order_mgr.cancel_order(pos.sl_order_id)
                            new_sl_oid = order_mgr.place_sl_order(
                                symbol, exit_dir, new_total_qty, pos.trailing_sl,
                            )
                            if new_sl_oid:
                                pos.sl_order_id = new_sl_oid
                        if pos.target_order_id:
                            order_mgr.cancel_order(pos.target_order_id)
                            new_tgt_oid = order_mgr.place_target_order(
                                symbol, exit_dir, new_total_qty, pos.target_price,
                            )
                            if new_tgt_oid:
                                pos.target_order_id = new_tgt_oid
                    else:
                        if fill_status != "REJECTED":
                            order_mgr.cancel_order(addon_oid)
            else:
                # Price reversed — skip remaining scale-ins
                log.info("Scale-in skipped for %s: price not confirming direction.", symbol)
                pos.scale_in_complete = True

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

    # Feed trade to adaptive engine for evolution analysis
    if adaptive:
        regime = "UNKNOWN"
        if strategy_engine:
            sig_info = strategy_engine.last_signals.get(trade["instrument"], {})
            regime = sig_info.get("regime", "UNKNOWN")
        adaptive.record_trade(trade, regime=regime)


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

    # Run end-of-day evolution cycle
    if adaptive:
        try:
            historical = adaptive.fetch_recent_trades()
            result = adaptive.evolve(db_trades=historical)
            if result.get("evolved"):
                log.info("Evolution result: %s", result)
                # Apply evolved weights for next trading day
                if strategy_engine:
                    adaptive.apply_to_engine(strategy_engine)
                adaptive.apply_param_overrides()
            adaptive.flush_daily_buffer()
        except Exception:
            log.exception("Evolution cycle failed — continuing with current params.")


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
    global strategy_engine, adaptive, scheduler

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

    # 4b. Initialize adaptive engine — loads evolved weights/params from disk
    adaptive = AdaptiveEngine()
    adaptive.load_state()
    adaptive.apply_to_engine(strategy_engine)
    adaptive.apply_param_overrides()
    # Give strategy engine access to regime memory for live weight boosts
    strategy_engine._adaptive_engine = adaptive
    log.info("Adaptive engine: %s", adaptive.status)

    # 5. Load previous-day close prices for gap detection
    log.info("Loading previous-day close prices for gap filter...")
    try:
        ohlc_data = kite.ohlc([f"{settings.EXCHANGE}:{sym}" for sym in token_map])
        for key, val in ohlc_data.items():
            sym = key.split(":")[-1]
            prev_close = val.get("ohlc", {}).get("close", 0)
            if prev_close > 0:
                # Will be set on the aggregator after DataFeed init
                pass
    except Exception as exc:
        log.warning("Failed to load prev-day closes for gap filter: %s", exc)
        ohlc_data = {}

    # 6. Start WebSocket data feed
    data_feed = DataFeed(
        access_token=kite.access_token,
        token_symbol_map=inv_map,
        on_candle=on_new_candle,
        candle_interval=settings.CANDLE_INTERVAL_MINUTES,
    )

    # Set previous-day close on aggregator for gap detection
    for key, val in ohlc_data.items():
        sym = key.split(":")[-1]
        prev_close = val.get("ohlc", {}).get("close", 0)
        if prev_close > 0:
            data_feed.aggregator.set_prev_day_close(sym, prev_close)

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
