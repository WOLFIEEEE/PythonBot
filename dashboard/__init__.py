"""
Web-based live dashboard for the trading bot.

Flask + Flask-SocketIO backend that provides:
  - Password-protected login (session-based)
  - REST API endpoints for bot state
  - Real-time WebSocket push for live updates
  - Serves the single-page dashboard frontend

Usage:
  from dashboard.app import start_dashboard
  start_dashboard(host="0.0.0.0", port=5000)
"""

from __future__ import annotations

import hashlib
import json
import os
import secrets
import threading
from datetime import datetime
from functools import wraps
from typing import Any

from flask import (
    Flask,
    jsonify,
    redirect,
    render_template,
    request,
    session,
    url_for,
)
from flask_socketio import SocketIO, disconnect, emit

from utils.logger import get_logger

log = get_logger(__name__)

# ── Flask app ─────────────────────────────────────────────────────────
app = Flask(
    __name__,
    template_folder=os.path.join(os.path.dirname(__file__), "templates"),
    static_folder=os.path.join(os.path.dirname(__file__), "static"),
)
app.config["SECRET_KEY"] = os.environ.get(
    "DASHBOARD_SECRET_KEY", secrets.token_hex(32)
)

socketio = SocketIO(app, cors_allowed_origins="*", async_mode="threading")

# ── Password configuration ────────────────────────────────────────────
# Password is stored as SHA-256 hash. Set via env var or config.
# Default: "admin123" — MUST be changed in production.
_DEFAULT_PASS_HASH = hashlib.sha256(b"admin123").hexdigest()
DASHBOARD_PASSWORD_HASH = os.environ.get(
    "DASHBOARD_PASSWORD_HASH", _DEFAULT_PASS_HASH
)

# ── References to bot internals (set by set_bot_references) ───────────
_bot_refs: dict[str, Any] = {
    "pos_tracker": None,
    "risk_mgr": None,
    "strategy_engine": None,
    "data_feed": None,
    "adaptive_engine": None,
    "order_mgr": None,
    "bot_running": False,
    "start_time": None,
}


def set_bot_references(**kwargs: Any) -> None:
    """Called from main.py to inject live bot object references."""
    _bot_refs.update(kwargs)
    log.info("Dashboard: bot references updated (%s)", list(kwargs.keys()))


# ── Auth helpers ──────────────────────────────────────────────────────
def _check_password(password: str) -> bool:
    return hashlib.sha256(password.encode()).hexdigest() == DASHBOARD_PASSWORD_HASH


def login_required(f):
    @wraps(f)
    def decorated(*args, **kwargs):
        if not session.get("authenticated"):
            return redirect(url_for("login"))
        return f(*args, **kwargs)
    return decorated


# ── Routes ────────────────────────────────────────────────────────────
@app.route("/login", methods=["GET", "POST"])
def login():
    error = None
    if request.method == "POST":
        password = request.form.get("password", "")
        if _check_password(password):
            session["authenticated"] = True
            session.permanent = True
            return redirect(url_for("dashboard"))
        error = "Invalid password"
    return render_template("login.html", error=error)


@app.route("/logout")
def logout():
    session.clear()
    return redirect(url_for("login"))


@app.route("/")
@login_required
def dashboard():
    return render_template("dashboard.html")


# ── REST API endpoints ────────────────────────────────────────────────
@app.route("/api/state")
@login_required
def api_state():
    """Full bot state snapshot."""
    return jsonify(_build_state())


@app.route("/api/positions")
@login_required
def api_positions():
    return jsonify(_get_positions())


@app.route("/api/trades")
@login_required
def api_trades():
    return jsonify(_get_closed_trades())


@app.route("/api/signals")
@login_required
def api_signals():
    engine = _bot_refs.get("strategy_engine")
    if not engine:
        return jsonify({})
    return jsonify(engine.last_signals)


@app.route("/api/adaptive")
@login_required
def api_adaptive():
    adaptive = _bot_refs.get("adaptive_engine")
    if not adaptive:
        return jsonify({})
    return jsonify(adaptive.status)


@app.route("/api/logs")
@login_required
def api_logs():
    """Return recent log lines from the log file."""
    from config import settings
    log_file = getattr(settings, "LOG_FILE", "logs/trading_bot.log")
    lines = []
    try:
        with open(log_file) as f:
            lines = f.readlines()[-200:]
    except FileNotFoundError:
        pass
    return jsonify({"lines": [l.rstrip() for l in lines]})


@app.route("/api/history")
@login_required
def api_history():
    """Return historical trades from DB for charts."""
    try:
        from utils.db import DailySummary, get_session
        sess = get_session()
        try:
            rows = sess.query(DailySummary).order_by(DailySummary.date.desc()).limit(30).all()
            return jsonify([{
                "date": r.date.isoformat() if r.date else "",
                "total_trades": r.total_trades,
                "wins": r.winning_trades,
                "losses": r.losing_trades,
                "net_pnl": r.net_pnl,
                "max_drawdown": r.max_drawdown,
            } for r in reversed(rows)])
        finally:
            sess.close()
    except Exception:
        return jsonify([])


# ── Control endpoints ─────────────────────────────────────────────────
@app.route("/api/emergency-squareoff", methods=["POST"])
@login_required
def api_emergency_squareoff():
    """Trigger emergency square-off of all positions."""
    try:
        import main as bot_main
        bot_main._square_off_all()
        return jsonify({"status": "ok", "message": "Emergency square-off triggered"})
    except Exception as e:
        return jsonify({"status": "error", "message": str(e)}), 500


# ── SocketIO events ──────────────────────────────────────────────────
@socketio.on("connect")
def handle_connect():
    if not session.get("authenticated"):
        disconnect()
        return
    emit("state_update", _build_state())
    log.debug("Dashboard client connected")


@socketio.on("request_update")
def handle_request_update():
    emit("state_update", _build_state())


# ── Data builders ────────────────────────────────────────────────────
def _build_state() -> dict[str, Any]:
    """Build a complete state snapshot for the dashboard."""
    pos_tracker = _bot_refs.get("pos_tracker")
    risk_mgr = _bot_refs.get("risk_mgr")
    engine = _bot_refs.get("strategy_engine")
    adaptive = _bot_refs.get("adaptive_engine")
    data_feed = _bot_refs.get("data_feed")

    # P&L summary
    realized = 0.0
    unrealized = 0.0
    total_trades = 0
    wins = 0
    losses = 0
    open_count = 0

    if pos_tracker:
        realized = pos_tracker.total_realized_pnl
        unrealized = pos_tracker.total_unrealized_pnl
        summary = pos_tracker.daily_summary
        total_trades = summary["total_trades"]
        wins = summary["winning_trades"]
        losses = summary["losing_trades"]
        open_count = len(pos_tracker.positions)

    # Risk info
    risk_info = {}
    if risk_mgr:
        from config import settings
        daily_pnl = realized + unrealized
        max_loss = settings.TOTAL_CAPITAL * settings.MAX_DAILY_LOSS_PCT / 100
        risk_info = {
            "trades_today": risk_mgr.trades_today,
            "max_trades": settings.MAX_TRADES_PER_DAY,
            "consecutive_losses": risk_mgr.consecutive_losses,
            "daily_pnl": round(daily_pnl, 2),
            "max_daily_loss": round(max_loss, 2),
            "loss_pct_used": round(abs(daily_pnl) / max_loss * 100, 1) if max_loss > 0 and daily_pnl < 0 else 0,
            "capital": settings.TOTAL_CAPITAL,
        }

    # Signals
    signals = {}
    if engine:
        signals = engine.last_signals

    # Adaptive engine
    adaptive_info = {}
    if adaptive:
        adaptive_info = adaptive.status

    # Data feed status
    feed_status = {}
    if data_feed:
        feed_status = {
            "connected": data_feed.is_connected,
        }

    return {
        "timestamp": datetime.now().isoformat(),
        "bot_running": _bot_refs.get("bot_running", False),
        "start_time": _bot_refs.get("start_time", ""),
        "pnl": {
            "realized": round(realized, 2),
            "unrealized": round(unrealized, 2),
            "total": round(realized + unrealized, 2),
        },
        "trades": {
            "total": total_trades,
            "wins": wins,
            "losses": losses,
            "win_rate": round(wins / total_trades * 100, 1) if total_trades > 0 else 0,
        },
        "open_positions": _get_positions(),
        "closed_trades": _get_closed_trades(),
        "signals": signals,
        "risk": risk_info,
        "adaptive": adaptive_info,
        "feed": feed_status,
        "open_count": open_count,
    }


def _get_positions() -> list[dict]:
    pos_tracker = _bot_refs.get("pos_tracker")
    if not pos_tracker:
        return []
    result = []
    for sym in pos_tracker.get_open_symbols():
        pos = pos_tracker.get_position(sym)
        if pos:
            result.append({
                "instrument": pos.instrument,
                "direction": pos.direction,
                "entry_price": round(pos.entry_price, 2),
                "quantity": pos.quantity,
                "sl_price": round(pos.sl_price, 2),
                "target_price": round(pos.target_price, 2),
                "trailing_sl": round(pos.trailing_sl, 2),
                "pnl": round(pos.current_pnl, 2),
                "strategy": pos.strategy,
                "entry_time": pos.entry_time.strftime("%H:%M:%S") if pos.entry_time else "",
                "scale_in_complete": pos.scale_in_complete,
            })
    return result


def _get_closed_trades() -> list[dict]:
    pos_tracker = _bot_refs.get("pos_tracker")
    if not pos_tracker:
        return []
    with pos_tracker._lock:
        trades = list(pos_tracker.closed_trades)
    return [
        {
            "instrument": t.get("instrument", ""),
            "direction": t.get("direction", ""),
            "entry_price": round(t.get("entry_price", 0), 2),
            "exit_price": round(t.get("exit_price", 0), 2),
            "quantity": t.get("quantity", 0),
            "net_pnl": round(t.get("net_pnl", 0), 2),
            "exit_reason": t.get("exit_reason", ""),
            "strategy": t.get("strategy", ""),
            "duration_min": t.get("duration_min", 0),
            "exit_time": t["exit_time"].strftime("%H:%M:%S")
            if hasattr(t.get("exit_time"), "strftime") else str(t.get("exit_time", "")),
        }
        for t in trades
    ]


# ── Broadcast push (called from bot internals) ───────────────────────
def push_update() -> None:
    """Push a state update to all connected dashboard clients."""
    try:
        socketio.emit("state_update", _build_state(), namespace="/")
    except Exception:
        pass  # Dashboard might not be running


def push_log(message: str, level: str = "INFO") -> None:
    """Push a log line to dashboard clients."""
    try:
        socketio.emit("log_line", {
            "timestamp": datetime.now().strftime("%H:%M:%S"),
            "level": level,
            "message": message,
        }, namespace="/")
    except Exception:
        pass


def push_trade_event(trade: dict, event_type: str = "trade") -> None:
    """Push a trade entry/exit event to dashboard clients."""
    try:
        socketio.emit("trade_event", {
            "type": event_type,
            "trade": trade,
            "timestamp": datetime.now().isoformat(),
        }, namespace="/")
    except Exception:
        pass


# ── Start dashboard server ───────────────────────────────────────────
_dashboard_thread: threading.Thread | None = None


def start_dashboard(host: str = "0.0.0.0", port: int = 5000) -> None:
    """Start the dashboard web server in a background thread."""
    global _dashboard_thread

    def _run():
        log.info("Dashboard starting on http://%s:%d", host, port)
        socketio.run(app, host=host, port=port, allow_unsafe_werkzeug=True)

    _dashboard_thread = threading.Thread(target=_run, daemon=True, name="DashboardThread")
    _dashboard_thread.start()
    log.info("Dashboard server thread started on port %d", port)


# ── Periodic broadcast (called by scheduler) ─────────────────────────
def broadcast_state() -> None:
    """Periodic state broadcast — called every few seconds by the scheduler."""
    push_update()
