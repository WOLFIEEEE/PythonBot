"""
Kite Intraday Trading Bot — GUI Launcher & Dashboard.

A tkinter-based application that:
  1. Setup Wizard — collects API credentials, capital, risk, strategies,
     watchlist, and notification settings before starting the bot.
  2. Live Dashboard — shows connection status, open positions, P&L,
     trade log, and bot logs in real time.
  3. Controls — Start / Stop / Emergency Square-Off buttons.
"""

from __future__ import annotations

import json
import os
import queue
import sys
import threading
import tkinter as tk
from datetime import datetime
from pathlib import Path
from tkinter import font as tkfont
from tkinter import messagebox, scrolledtext, ttk
from typing import Any

# ---------------------------------------------------------------------------
# Resolve project root so imports work regardless of cwd.
# ---------------------------------------------------------------------------
PROJECT_ROOT = str(Path(__file__).resolve().parent.parent)
if PROJECT_ROOT not in sys.path:
    sys.path.insert(0, PROJECT_ROOT)

# ---------------------------------------------------------------------------
# Colour palette — dark professional trading terminal aesthetic
# ---------------------------------------------------------------------------
BG           = "#0d1117"
BG_CARD      = "#161b22"
BG_INPUT     = "#21262d"
FG           = "#c9d1d9"
FG_DIM       = "#8b949e"
FG_BRIGHT    = "#f0f6fc"
ACCENT       = "#58a6ff"
GREEN        = "#3fb950"
RED          = "#f85149"
ORANGE       = "#d29922"
BORDER       = "#30363d"
BTN_BG       = "#21262d"
BTN_HOVER    = "#30363d"

# ---------------------------------------------------------------------------
# Default parameters (mirrors config/settings.py)
# ---------------------------------------------------------------------------
DEFAULT_WATCHLIST = [
    "NSE:RELIANCE", "NSE:TCS", "NSE:INFY", "NSE:HDFCBANK",
    "NSE:ICICIBANK", "NSE:SBIN", "NSE:BHARTIARTL", "NSE:ITC",
    "NSE:KOTAKBANK", "NSE:LT",
]

STRATEGY_OPTIONS = [
    ("EMA Crossover", "ema_crossover", True),
    ("Supertrend", "supertrend", True),
    ("VWAP Breakout", "vwap_breakout", True),
    ("Opening Range Breakout", "orb", True),
]

CONFIG_FILE = os.path.join(PROJECT_ROOT, "gui", "last_config.json")


# ═══════════════════════════════════════════════════════════════════════════
#  Helper: write config/.env from user inputs
# ═══════════════════════════════════════════════════════════════════════════
def _write_env_file(params: dict[str, Any]) -> None:
    env_path = os.path.join(PROJECT_ROOT, "config", ".env")
    lines = [
        f'KITE_API_KEY={params["api_key"]}',
        f'KITE_API_SECRET={params["api_secret"]}',
        f'TELEGRAM_BOT_TOKEN={params.get("tg_token", "")}',
        f'TELEGRAM_CHAT_ID={params.get("tg_chat_id", "")}',
        f'DATABASE_URL={params.get("db_url", "sqlite:///trades.db")}',
        "LOG_LEVEL=INFO",
        "LOG_FILE=logs/trading_bot.log",
    ]
    with open(env_path, "w") as fh:
        fh.write("\n".join(lines) + "\n")


def _patch_settings(params: dict[str, Any]) -> None:
    """Write a settings_override.json that main.py reads at startup."""
    override = {
        "TOTAL_CAPITAL": params.get("capital", 100_000),
        "RISK_PER_TRADE_PCT": params.get("risk_pct", 1.0),
        "MAX_TRADES_PER_DAY": params.get("max_trades", 5),
        "MAX_DAILY_LOSS_PCT": params.get("max_loss_pct", 3.0),
        "MAX_OPEN_POSITIONS": params.get("max_positions", 3),
        "SL_PCT": params.get("sl_pct", 0.5),
        "TARGET_PCT": params.get("target_pct", 1.0),
        "TRAILING_SL": params.get("trailing_sl", True),
        "TRAILING_SL_PCT": params.get("trailing_sl_pct", 0.3),
        "CANDLE_INTERVAL_MINUTES": params.get("candle_interval", 5),
        "SQUARE_OFF_TIME": params.get("square_off_time", "15:10"),
        "NO_NEW_TRADES_AFTER": params.get("no_new_after", "14:30"),
        "WATCHLIST": params.get("watchlist", DEFAULT_WATCHLIST),
        "MIN_CONFLUENCE": params.get("min_confluence", 2),
        "STRATEGIES": params.get("strategies", ["ema_crossover", "supertrend", "vwap_breakout", "orb"]),
    }
    path = os.path.join(PROJECT_ROOT, "config", "settings_override.json")
    with open(path, "w") as fh:
        json.dump(override, fh, indent=2)


def _save_gui_config(params: dict[str, Any]) -> None:
    """Persist last-used GUI config for quick reload."""
    safe = {k: v for k, v in params.items() if k != "api_secret"}
    with open(CONFIG_FILE, "w") as fh:
        json.dump(safe, fh, indent=2)


def _load_gui_config() -> dict[str, Any]:
    if os.path.exists(CONFIG_FILE):
        try:
            with open(CONFIG_FILE) as fh:
                return json.load(fh)
        except Exception:
            pass
    return {}


# ═══════════════════════════════════════════════════════════════════════════
#  Main Application
# ═══════════════════════════════════════════════════════════════════════════
class TradingBotGUI(tk.Tk):
    def __init__(self) -> None:
        super().__init__()
        self.title("Kite Intraday Trading Bot")
        self.geometry("1100x750")
        self.minsize(900, 650)
        self.configure(bg=BG)
        self.protocol("WM_DELETE_WINDOW", self._on_close)

        # ── State ──
        self._bot_thread: threading.Thread | None = None
        self._bot_running = False
        self._log_queue: queue.Queue[str] = queue.Queue()
        self._params: dict[str, Any] = {}
        self._saved = _load_gui_config()

        # ── Fonts ──
        self._title_font = tkfont.Font(family="Helvetica", size=16, weight="bold")
        self._heading_font = tkfont.Font(family="Helvetica", size=11, weight="bold")
        self._body_font = tkfont.Font(family="Helvetica", size=10)
        self._mono_font = tkfont.Font(family="Courier", size=10)
        self._small_font = tkfont.Font(family="Helvetica", size=9)

        # ── Style ──
        self._setup_styles()

        # ── Container ──
        self._container = tk.Frame(self, bg=BG)
        self._container.pack(fill="both", expand=True)

        # Start on the setup wizard
        self._show_setup_wizard()

    # ───────────────────────────────────────────────────────────────────
    #  ttk style configuration
    # ───────────────────────────────────────────────────────────────────
    def _setup_styles(self) -> None:
        style = ttk.Style(self)
        style.theme_use("clam")

        style.configure(".", background=BG, foreground=FG, font=self._body_font)
        style.configure("TFrame", background=BG)
        style.configure("Card.TFrame", background=BG_CARD, relief="flat")
        style.configure("TLabel", background=BG, foreground=FG, font=self._body_font)
        style.configure("Dim.TLabel", background=BG, foreground=FG_DIM, font=self._small_font)
        style.configure("Card.TLabel", background=BG_CARD, foreground=FG, font=self._body_font)
        style.configure("Heading.TLabel", background=BG, foreground=FG_BRIGHT, font=self._heading_font)
        style.configure("Title.TLabel", background=BG, foreground=ACCENT, font=self._title_font)
        style.configure("Green.TLabel", background=BG_CARD, foreground=GREEN, font=self._heading_font)
        style.configure("Red.TLabel", background=BG_CARD, foreground=RED, font=self._heading_font)

        style.configure("TEntry", fieldbackground=BG_INPUT, foreground=FG_BRIGHT,
                         insertcolor=FG_BRIGHT, borderwidth=1, relief="solid")
        style.map("TEntry", fieldbackground=[("focus", BG_INPUT)])

        style.configure("TSpinbox", fieldbackground=BG_INPUT, foreground=FG_BRIGHT,
                         arrowcolor=FG_DIM, borderwidth=1, relief="solid")

        style.configure("TCheckbutton", background=BG, foreground=FG, font=self._body_font)
        style.map("TCheckbutton", background=[("active", BG)])

        style.configure("Accent.TButton", background=ACCENT, foreground="#ffffff",
                         font=self._heading_font, padding=(20, 10), borderwidth=0)
        style.map("Accent.TButton", background=[("active", "#79c0ff")])

        style.configure("Danger.TButton", background=RED, foreground="#ffffff",
                         font=self._heading_font, padding=(20, 10), borderwidth=0)
        style.map("Danger.TButton", background=[("active", "#ff7b72")])

        style.configure("Secondary.TButton", background=BTN_BG, foreground=FG,
                         font=self._body_font, padding=(14, 8), borderwidth=1)
        style.map("Secondary.TButton", background=[("active", BTN_HOVER)])

        style.configure("TNotebook", background=BG, borderwidth=0)
        style.configure("TNotebook.Tab", background=BG_CARD, foreground=FG_DIM,
                         padding=(16, 8), font=self._body_font)
        style.map("TNotebook.Tab",
                  background=[("selected", BG)],
                  foreground=[("selected", ACCENT)])

        # Treeview for positions/trades
        style.configure("Treeview", background=BG_CARD, fieldbackground=BG_CARD,
                         foreground=FG, font=self._body_font, rowheight=28, borderwidth=0)
        style.configure("Treeview.Heading", background=BG_INPUT, foreground=FG_DIM,
                         font=self._body_font, borderwidth=0)
        style.map("Treeview", background=[("selected", "#1f6feb")],
                  foreground=[("selected", "#ffffff")])

    # ───────────────────────────────────────────────────────────────────
    #  Utility: clear container
    # ───────────────────────────────────────────────────────────────────
    def _clear(self) -> None:
        for w in self._container.winfo_children():
            w.destroy()

    # ═══════════════════════════════════════════════════════════════════
    #  SETUP WIZARD
    # ═══════════════════════════════════════════════════════════════════
    def _show_setup_wizard(self) -> None:
        self._clear()

        # Scrollable canvas for long forms
        canvas = tk.Canvas(self._container, bg=BG, highlightthickness=0)
        scrollbar = ttk.Scrollbar(self._container, orient="vertical", command=canvas.yview)
        scroll_frame = tk.Frame(canvas, bg=BG)

        scroll_frame.bind("<Configure>", lambda e: canvas.configure(scrollregion=canvas.bbox("all")))
        canvas.create_window((0, 0), window=scroll_frame, anchor="nw")
        canvas.configure(yscrollcommand=scrollbar.set)

        canvas.pack(side="left", fill="both", expand=True, padx=20, pady=10)
        scrollbar.pack(side="right", fill="y")

        # Mouse-wheel scroll
        def _on_mousewheel(event):
            canvas.yview_scroll(int(-1 * (event.delta / 120)), "units")
        canvas.bind_all("<MouseWheel>", _on_mousewheel)
        canvas.bind_all("<Button-4>", lambda e: canvas.yview_scroll(-3, "units"))
        canvas.bind_all("<Button-5>", lambda e: canvas.yview_scroll(3, "units"))

        parent = scroll_frame
        s = self._saved

        # ── Title ──
        ttk.Label(parent, text="Kite Intraday Trading Bot", style="Title.TLabel").pack(anchor="w", pady=(10, 2))
        ttk.Label(parent, text="Configure your bot parameters below, then click Start Trading.",
                  style="Dim.TLabel").pack(anchor="w", pady=(0, 18))

        # ── Section 1: API Credentials ──
        self._section(parent, "1. Kite Connect API Credentials")
        cred_frame = self._card(parent)

        ttk.Label(cred_frame, text="API Key", style="Card.TLabel").grid(row=0, column=0, sticky="w", padx=8, pady=4)
        self._api_key_var = tk.StringVar(value=s.get("api_key", ""))
        ttk.Entry(cred_frame, textvariable=self._api_key_var, width=48).grid(row=0, column=1, padx=8, pady=4)

        ttk.Label(cred_frame, text="API Secret", style="Card.TLabel").grid(row=1, column=0, sticky="w", padx=8, pady=4)
        self._api_secret_var = tk.StringVar()
        secret_entry = ttk.Entry(cred_frame, textvariable=self._api_secret_var, width=48, show="*")
        secret_entry.grid(row=1, column=1, padx=8, pady=4)

        ttk.Label(cred_frame, text="Credentials are saved to config/.env (never committed to git).",
                  style="Dim.TLabel").grid(row=2, column=0, columnspan=2, sticky="w", padx=8, pady=(0, 6))

        # ── Section 2: Capital & Risk ──
        self._section(parent, "2. Capital & Risk Management")
        risk_frame = self._card(parent)

        fields_risk = [
            ("Total Capital (INR)", "capital", s.get("capital", 100000), 10000, 10000000),
            ("Risk per Trade (%)", "risk_pct", s.get("risk_pct", 1.0), 0.1, 5.0),
            ("Max Trades / Day", "max_trades", s.get("max_trades", 5), 1, 20),
            ("Max Daily Loss (%)", "max_loss_pct", s.get("max_loss_pct", 3.0), 0.5, 10.0),
            ("Max Open Positions", "max_positions", s.get("max_positions", 3), 1, 10),
            ("Stop-Loss (%)", "sl_pct", s.get("sl_pct", 0.5), 0.1, 5.0),
            ("Target (%)", "target_pct", s.get("target_pct", 1.0), 0.1, 10.0),
            ("Trailing SL (%)", "trailing_sl_pct", s.get("trailing_sl_pct", 0.3), 0.05, 3.0),
        ]
        self._spin_vars: dict[str, tk.DoubleVar] = {}
        for i, (label, key, default, lo, hi) in enumerate(fields_risk):
            ttk.Label(risk_frame, text=label, style="Card.TLabel").grid(
                row=i, column=0, sticky="w", padx=8, pady=3)
            var = tk.DoubleVar(value=default)
            self._spin_vars[key] = var
            ttk.Spinbox(risk_frame, from_=lo, to=hi, textvariable=var,
                        width=12, increment=0.1 if isinstance(default, float) else 1
                        ).grid(row=i, column=1, sticky="w", padx=8, pady=3)

        self._trailing_sl_var = tk.BooleanVar(value=s.get("trailing_sl", True))
        ttk.Checkbutton(risk_frame, text="Enable Trailing Stop-Loss",
                        variable=self._trailing_sl_var).grid(
            row=len(fields_risk), column=0, columnspan=2, sticky="w", padx=8, pady=4)

        # Risk-reward preview
        self._rr_label = ttk.Label(risk_frame, text="", style="Card.TLabel")
        self._rr_label.grid(row=len(fields_risk)+1, column=0, columnspan=2, sticky="w", padx=8, pady=4)
        self._update_rr_preview()
        self._spin_vars["sl_pct"].trace_add("write", lambda *_: self._update_rr_preview())
        self._spin_vars["target_pct"].trace_add("write", lambda *_: self._update_rr_preview())

        # ── Section 3: Timing ──
        self._section(parent, "3. Market Timing (IST)")
        time_frame = self._card(parent)

        time_fields = [
            ("Square-Off Time", "square_off_time", s.get("square_off_time", "15:10")),
            ("No New Trades After", "no_new_after", s.get("no_new_after", "14:30")),
            ("Candle Interval (min)", "candle_interval", s.get("candle_interval", "5")),
        ]
        self._time_vars: dict[str, tk.StringVar] = {}
        for i, (label, key, default) in enumerate(time_fields):
            ttk.Label(time_frame, text=label, style="Card.TLabel").grid(
                row=i, column=0, sticky="w", padx=8, pady=3)
            var = tk.StringVar(value=str(default))
            self._time_vars[key] = var
            ttk.Entry(time_frame, textvariable=var, width=12).grid(
                row=i, column=1, sticky="w", padx=8, pady=3)

        # ── Section 4: Strategies ──
        self._section(parent, "4. Strategy Selection")
        strat_frame = self._card(parent)

        ttk.Label(strat_frame, text="Select which strategies to run (min 2 for confluence):",
                  style="Card.TLabel").pack(anchor="w", padx=8, pady=(6, 2))

        self._strat_vars: dict[str, tk.BooleanVar] = {}
        saved_strats = s.get("strategies", [opt[1] for opt in STRATEGY_OPTIONS])
        for name, key, _ in STRATEGY_OPTIONS:
            var = tk.BooleanVar(value=(key in saved_strats))
            self._strat_vars[key] = var
            ttk.Checkbutton(strat_frame, text=name, variable=var).pack(
                anchor="w", padx=20, pady=1)

        conf_frame = tk.Frame(strat_frame, bg=BG_CARD)
        conf_frame.pack(anchor="w", padx=8, pady=6)
        ttk.Label(conf_frame, text="Min Confluence (strategies must agree):", style="Card.TLabel").pack(side="left")
        self._confluence_var = tk.IntVar(value=s.get("min_confluence", 2))
        ttk.Spinbox(conf_frame, from_=1, to=4, textvariable=self._confluence_var,
                    width=5).pack(side="left", padx=6)

        # ── Section 5: Watchlist ──
        self._section(parent, "5. Watchlist")
        wl_frame = self._card(parent)

        ttk.Label(wl_frame, text="Enter instruments (one per line, format: NSE:SYMBOL):",
                  style="Card.TLabel").pack(anchor="w", padx=8, pady=(6, 2))

        self._watchlist_text = tk.Text(wl_frame, height=8, width=50,
                                       bg=BG_INPUT, fg=FG_BRIGHT, insertbackground=FG_BRIGHT,
                                       font=self._mono_font, relief="solid", bd=1,
                                       highlightbackground=BORDER, highlightthickness=1)
        self._watchlist_text.pack(padx=8, pady=4)
        wl = s.get("watchlist", DEFAULT_WATCHLIST)
        self._watchlist_text.insert("1.0", "\n".join(wl))

        # ── Section 6: Telegram ──
        self._section(parent, "6. Telegram Notifications (Optional)")
        tg_frame = self._card(parent)

        ttk.Label(tg_frame, text="Bot Token", style="Card.TLabel").grid(
            row=0, column=0, sticky="w", padx=8, pady=4)
        self._tg_token_var = tk.StringVar(value=s.get("tg_token", ""))
        ttk.Entry(tg_frame, textvariable=self._tg_token_var, width=48).grid(
            row=0, column=1, padx=8, pady=4)

        ttk.Label(tg_frame, text="Chat ID", style="Card.TLabel").grid(
            row=1, column=0, sticky="w", padx=8, pady=4)
        self._tg_chat_var = tk.StringVar(value=s.get("tg_chat_id", ""))
        ttk.Entry(tg_frame, textvariable=self._tg_chat_var, width=48).grid(
            row=1, column=1, padx=8, pady=4)

        ttk.Label(tg_frame, text="Leave empty to skip Telegram notifications.",
                  style="Dim.TLabel").grid(row=2, column=0, columnspan=2, sticky="w", padx=8, pady=(0, 6))

        # ── Start Button ──
        btn_frame = tk.Frame(parent, bg=BG)
        btn_frame.pack(pady=20)
        ttk.Button(btn_frame, text="  Start Trading  ", style="Accent.TButton",
                   command=self._on_start).pack(side="left", padx=8)
        ttk.Button(btn_frame, text="  Reset Defaults  ", style="Secondary.TButton",
                   command=self._reset_defaults).pack(side="left", padx=8)

        # Bottom padding
        tk.Frame(parent, bg=BG, height=30).pack()

    def _section(self, parent: tk.Widget, text: str) -> None:
        ttk.Label(parent, text=text, style="Heading.TLabel").pack(
            anchor="w", pady=(14, 4))

    def _card(self, parent: tk.Widget) -> tk.Frame:
        frame = tk.Frame(parent, bg=BG_CARD, highlightbackground=BORDER,
                         highlightthickness=1, padx=12, pady=8)
        frame.pack(fill="x", pady=4)
        return frame

    def _update_rr_preview(self) -> None:
        try:
            sl = self._spin_vars["sl_pct"].get()
            tgt = self._spin_vars["target_pct"].get()
            if sl > 0:
                rr = tgt / sl
                self._rr_label.configure(text=f"Risk : Reward = 1 : {rr:.1f}")
            else:
                self._rr_label.configure(text="")
        except Exception:
            pass

    def _reset_defaults(self) -> None:
        self._spin_vars["capital"].set(100000)
        self._spin_vars["risk_pct"].set(1.0)
        self._spin_vars["max_trades"].set(5)
        self._spin_vars["max_loss_pct"].set(3.0)
        self._spin_vars["max_positions"].set(3)
        self._spin_vars["sl_pct"].set(0.5)
        self._spin_vars["target_pct"].set(1.0)
        self._spin_vars["trailing_sl_pct"].set(0.3)
        self._trailing_sl_var.set(True)
        self._time_vars["square_off_time"].set("15:10")
        self._time_vars["no_new_after"].set("14:30")
        self._time_vars["candle_interval"].set("5")
        self._confluence_var.set(2)
        for key, var in self._strat_vars.items():
            var.set(True)
        self._watchlist_text.delete("1.0", "end")
        self._watchlist_text.insert("1.0", "\n".join(DEFAULT_WATCHLIST))
        self._tg_token_var.set("")
        self._tg_chat_var.set("")

    # ───────────────────────────────────────────────────────────────────
    #  Validate & collect params
    # ───────────────────────────────────────────────────────────────────
    def _collect_params(self) -> dict[str, Any] | None:
        api_key = self._api_key_var.get().strip()
        api_secret = self._api_secret_var.get().strip()
        if not api_key or not api_secret:
            messagebox.showerror("Missing Credentials",
                                 "API Key and API Secret are required.")
            return None

        strategies = [k for k, v in self._strat_vars.items() if v.get()]
        if len(strategies) < 1:
            messagebox.showerror("No Strategies",
                                 "Select at least one strategy.")
            return None

        confluence = self._confluence_var.get()
        if confluence > len(strategies):
            messagebox.showerror("Confluence Error",
                                 f"Min confluence ({confluence}) cannot exceed "
                                 f"selected strategies ({len(strategies)}).")
            return None

        wl_text = self._watchlist_text.get("1.0", "end").strip()
        watchlist = [line.strip() for line in wl_text.splitlines() if line.strip()]
        if not watchlist:
            messagebox.showerror("Empty Watchlist",
                                 "Add at least one instrument to the watchlist.")
            return None

        try:
            candle_interval = int(self._time_vars["candle_interval"].get())
        except ValueError:
            messagebox.showerror("Invalid Input", "Candle interval must be a number.")
            return None

        params = {
            "api_key": api_key,
            "api_secret": api_secret,
            "capital": self._spin_vars["capital"].get(),
            "risk_pct": self._spin_vars["risk_pct"].get(),
            "max_trades": int(self._spin_vars["max_trades"].get()),
            "max_loss_pct": self._spin_vars["max_loss_pct"].get(),
            "max_positions": int(self._spin_vars["max_positions"].get()),
            "sl_pct": self._spin_vars["sl_pct"].get(),
            "target_pct": self._spin_vars["target_pct"].get(),
            "trailing_sl": self._trailing_sl_var.get(),
            "trailing_sl_pct": self._spin_vars["trailing_sl_pct"].get(),
            "square_off_time": self._time_vars["square_off_time"].get().strip(),
            "no_new_after": self._time_vars["no_new_after"].get().strip(),
            "candle_interval": candle_interval,
            "strategies": strategies,
            "min_confluence": confluence,
            "watchlist": watchlist,
            "tg_token": self._tg_token_var.get().strip(),
            "tg_chat_id": self._tg_chat_var.get().strip(),
            "db_url": "sqlite:///trades.db",
        }
        return params

    # ───────────────────────────────────────────────────────────────────
    #  Start button handler
    # ───────────────────────────────────────────────────────────────────
    def _on_start(self) -> None:
        params = self._collect_params()
        if params is None:
            return

        self._params = params

        # Persist configs
        _write_env_file(params)
        _patch_settings(params)
        _save_gui_config(params)

        # Ensure logs directory
        os.makedirs(os.path.join(PROJECT_ROOT, "logs"), exist_ok=True)

        # Transition to dashboard
        self._show_dashboard()

        # Launch bot in background thread
        self._start_bot()

    # ═══════════════════════════════════════════════════════════════════
    #  LIVE DASHBOARD
    # ═══════════════════════════════════════════════════════════════════
    def _show_dashboard(self) -> None:
        self._clear()

        # Unbind mousewheel from wizard
        self.unbind_all("<MouseWheel>")
        self.unbind_all("<Button-4>")
        self.unbind_all("<Button-5>")

        # ── Top bar ──
        top = tk.Frame(self._container, bg=BG_CARD, height=56)
        top.pack(fill="x", padx=0, pady=0)
        top.pack_propagate(False)

        ttk.Label(top, text="  Kite Intraday Bot", style="Title.TLabel",
                  background=BG_CARD).pack(side="left", padx=12)

        self._status_label = ttk.Label(top, text="  STARTING...", style="Card.TLabel",
                                        foreground=ORANGE, background=BG_CARD)
        self._status_label.pack(side="left", padx=20)

        self._clock_label = ttk.Label(top, text="", style="Card.TLabel",
                                       foreground=FG_DIM, background=BG_CARD)
        self._clock_label.pack(side="right", padx=16)

        # ── Control buttons ──
        btn_bar = tk.Frame(self._container, bg=BG)
        btn_bar.pack(fill="x", padx=12, pady=8)

        ttk.Button(btn_bar, text="  Stop Bot  ", style="Danger.TButton",
                   command=self._on_stop).pack(side="left", padx=4)
        ttk.Button(btn_bar, text="  Emergency Square-Off  ", style="Danger.TButton",
                   command=self._on_emergency_squareoff).pack(side="left", padx=4)
        ttk.Button(btn_bar, text="  Back to Setup  ", style="Secondary.TButton",
                   command=self._on_back_to_setup).pack(side="right", padx=4)

        # ── P&L Summary Cards ──
        cards = tk.Frame(self._container, bg=BG)
        cards.pack(fill="x", padx=12, pady=4)

        self._pnl_cards: dict[str, ttk.Label] = {}
        for label in ["Realized P&L", "Unrealized P&L", "Total P&L",
                       "Trades Today", "Open Positions", "Win Rate"]:
            card = tk.Frame(cards, bg=BG_CARD, highlightbackground=BORDER,
                            highlightthickness=1, padx=14, pady=8)
            card.pack(side="left", fill="both", expand=True, padx=3)
            ttk.Label(card, text=label, style="Dim.TLabel",
                      background=BG_CARD).pack(anchor="w")
            val = ttk.Label(card, text="--", style="Green.TLabel", background=BG_CARD)
            val.pack(anchor="w")
            self._pnl_cards[label] = val

        # ── Notebook: Positions / Trade Log / Bot Log ──
        nb = ttk.Notebook(self._container)
        nb.pack(fill="both", expand=True, padx=12, pady=8)

        # Tab 1: Positions
        pos_frame = tk.Frame(nb, bg=BG)
        nb.add(pos_frame, text="  Open Positions  ")

        pos_cols = ("Symbol", "Direction", "Entry", "Qty", "SL", "Target", "P&L", "Strategy")
        self._pos_tree = ttk.Treeview(pos_frame, columns=pos_cols, show="headings", height=6)
        for col in pos_cols:
            self._pos_tree.heading(col, text=col)
            self._pos_tree.column(col, width=100, anchor="center")
        self._pos_tree.pack(fill="both", expand=True, padx=4, pady=4)

        # Tab 2: Trade Log
        log_frame = tk.Frame(nb, bg=BG)
        nb.add(log_frame, text="  Trade Log  ")

        log_cols = ("Time", "Symbol", "Dir", "Entry", "Exit", "P&L", "Reason", "Strategy")
        self._trade_tree = ttk.Treeview(log_frame, columns=log_cols, show="headings", height=8)
        for col in log_cols:
            self._trade_tree.heading(col, text=col)
            self._trade_tree.column(col, width=95, anchor="center")
        self._trade_tree.pack(fill="both", expand=True, padx=4, pady=4)

        # Tab 3: Bot Log
        bot_log_frame = tk.Frame(nb, bg=BG)
        nb.add(bot_log_frame, text="  Bot Log  ")

        self._log_text = scrolledtext.ScrolledText(
            bot_log_frame, bg=BG_CARD, fg=FG, font=self._mono_font,
            insertbackground=FG, state="disabled", wrap="word",
            relief="flat", highlightthickness=0)
        self._log_text.pack(fill="both", expand=True, padx=4, pady=4)

        # Configure log text colour tags
        self._log_text.tag_configure("INFO", foreground=FG)
        self._log_text.tag_configure("WARNING", foreground=ORANGE)
        self._log_text.tag_configure("ERROR", foreground=RED)
        self._log_text.tag_configure("TRADE", foreground=GREEN)

        # Start periodic update
        self._update_dashboard()

    # ───────────────────────────────────────────────────────────────────
    #  Dashboard periodic refresh
    # ───────────────────────────────────────────────────────────────────
    def _update_dashboard(self) -> None:
        if not self._container.winfo_exists():
            return

        # Update clock
        try:
            from utils.helpers import now_ist
            now = now_ist().strftime("%H:%M:%S IST")
        except Exception:
            now = datetime.now().strftime("%H:%M:%S")
        self._clock_label.configure(text=now)

        # Update status
        if self._bot_running:
            self._status_label.configure(text="  RUNNING", foreground=GREEN)
        else:
            self._status_label.configure(text="  STOPPED", foreground=RED)

        # Drain log queue
        while not self._log_queue.empty():
            try:
                msg = self._log_queue.get_nowait()
                self._append_log(msg)
            except queue.Empty:
                break

        # Update P&L cards from bot state
        self._refresh_pnl_cards()
        self._refresh_positions()
        self._refresh_trades()

        # Schedule next update
        self.after(1000, self._update_dashboard)

    def _append_log(self, msg: str) -> None:
        self._log_text.configure(state="normal")
        tag = "INFO"
        if "WARNING" in msg or "warning" in msg.lower():
            tag = "WARNING"
        elif "ERROR" in msg or "error" in msg.lower():
            tag = "ERROR"
        elif "order placed" in msg.lower() or "position" in msg.lower():
            tag = "TRADE"
        self._log_text.insert("end", msg + "\n", tag)
        self._log_text.see("end")
        self._log_text.configure(state="disabled")

    def _refresh_pnl_cards(self) -> None:
        try:
            from gui.bot_runner import get_bot_state
            state = get_bot_state()
            if state is None:
                return

            realized = state.get("realized_pnl", 0)
            unrealized = state.get("unrealized_pnl", 0)
            total = realized + unrealized
            trades = state.get("trades_today", 0)
            open_pos = state.get("open_positions", 0)
            wins = state.get("wins", 0)
            win_rate = f"{wins / trades * 100:.0f}%" if trades > 0 else "--"

            self._set_pnl_card("Realized P&L", f"Rs.{realized:+,.0f}", realized)
            self._set_pnl_card("Unrealized P&L", f"Rs.{unrealized:+,.0f}", unrealized)
            self._set_pnl_card("Total P&L", f"Rs.{total:+,.0f}", total)
            self._pnl_cards["Trades Today"].configure(text=str(trades), foreground=FG_BRIGHT)
            self._pnl_cards["Open Positions"].configure(text=str(open_pos), foreground=ACCENT)
            self._pnl_cards["Win Rate"].configure(text=win_rate, foreground=FG_BRIGHT)
        except Exception:
            pass

    def _set_pnl_card(self, label: str, text: str, value: float) -> None:
        colour = GREEN if value >= 0 else RED
        self._pnl_cards[label].configure(text=text, foreground=colour)

    def _refresh_positions(self) -> None:
        try:
            from gui.bot_runner import get_open_positions
            positions = get_open_positions()
        except Exception:
            return

        for item in self._pos_tree.get_children():
            self._pos_tree.delete(item)

        for p in positions:
            colour_tag = "green" if p.get("pnl", 0) >= 0 else "red"
            self._pos_tree.insert("", "end", values=(
                p.get("instrument", ""),
                p.get("direction", ""),
                f"{p.get('entry_price', 0):.2f}",
                p.get("quantity", 0),
                f"{p.get('sl_price', 0):.2f}",
                f"{p.get('target_price', 0):.2f}",
                f"{p.get('pnl', 0):+,.0f}",
                p.get("strategy", ""),
            ))

    def _refresh_trades(self) -> None:
        try:
            from gui.bot_runner import get_closed_trades
            trades = get_closed_trades()
        except Exception:
            return

        for item in self._trade_tree.get_children():
            self._trade_tree.delete(item)

        for t in trades:
            exit_time = t.get("exit_time")
            time_str = exit_time.strftime("%H:%M") if hasattr(exit_time, "strftime") else str(exit_time)
            self._trade_tree.insert("", "end", values=(
                time_str,
                t.get("instrument", ""),
                t.get("direction", ""),
                f"{t.get('entry_price', 0):.2f}",
                f"{t.get('exit_price', 0):.2f}",
                f"{t.get('net_pnl', 0):+,.0f}",
                t.get("exit_reason", ""),
                t.get("strategy", ""),
            ))

    # ───────────────────────────────────────────────────────────────────
    #  Bot lifecycle
    # ───────────────────────────────────────────────────────────────────
    def _start_bot(self) -> None:
        self._bot_running = True
        self._bot_thread = threading.Thread(
            target=self._run_bot, daemon=True, name="BotThread"
        )
        self._bot_thread.start()
        self._append_log("[GUI] Bot thread started.")

    def _run_bot(self) -> None:
        try:
            from gui.bot_runner import run_bot
            run_bot(self._params, self._log_queue)
        except Exception as exc:
            self._log_queue.put(f"[ERROR] Bot crashed: {exc}")
        finally:
            self._bot_running = False

    def _on_stop(self) -> None:
        if not self._bot_running:
            return
        try:
            from gui.bot_runner import stop_bot
            stop_bot()
            self._append_log("[GUI] Stop signal sent.")
        except Exception as exc:
            self._append_log(f"[ERROR] Stop failed: {exc}")

    def _on_emergency_squareoff(self) -> None:
        if not messagebox.askyesno("Confirm",
                                    "Emergency square-off all positions?\n"
                                    "This will close everything immediately."):
            return
        try:
            from gui.bot_runner import emergency_square_off
            emergency_square_off()
            self._append_log("[GUI] Emergency square-off triggered!")
        except Exception as exc:
            self._append_log(f"[ERROR] Square-off failed: {exc}")

    def _on_back_to_setup(self) -> None:
        if self._bot_running:
            if not messagebox.askyesno("Bot Running",
                                        "Bot is still running. Stop it and go back to setup?"):
                return
            self._on_stop()
        self._show_setup_wizard()

    def _on_close(self) -> None:
        if self._bot_running:
            if messagebox.askyesno("Quit",
                                    "Bot is running. Stop and quit?"):
                self._on_stop()
            else:
                return
        self.destroy()


# ═══════════════════════════════════════════════════════════════════════════
#  Entry point
# ═══════════════════════════════════════════════════════════════════════════
def main() -> None:
    app = TradingBotGUI()
    app.mainloop()


if __name__ == "__main__":
    main()
