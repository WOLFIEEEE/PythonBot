"""
Trading bot configuration — all tunable parameters.
"""

import os
from dotenv import load_dotenv

load_dotenv(os.path.join(os.path.dirname(__file__), ".env"))

# ── Kite Connect Credentials ──────────────────────────────────────────
KITE_API_KEY = os.getenv("KITE_API_KEY", "")
KITE_API_SECRET = os.getenv("KITE_API_SECRET", "")

# ── Capital & Risk ────────────────────────────────────────────────────
TOTAL_CAPITAL = 100_000          # Total capital allocated (INR)
RISK_PER_TRADE_PCT = 1.0         # Risk 1% of capital per trade
MAX_TRADES_PER_DAY = 5           # Maximum trades in a session
MAX_DAILY_LOSS_PCT = 3.0         # Stop trading if daily loss exceeds 3%
MAX_OPEN_POSITIONS = 3           # Maximum concurrent positions

# ── Market Timing (IST) ──────────────────────────────────────────────
MARKET_OPEN = "09:15"
MARKET_CLOSE = "15:30"
SQUARE_OFF_TIME = "15:10"        # Auto square-off 20 min before close
NO_NEW_TRADES_AFTER = "14:30"    # Stop new entries after this time

# ── Strategy Parameters ──────────────────────────────────────────────
EMA_FAST = 9
EMA_SLOW = 21
SUPERTREND_PERIOD = 10
SUPERTREND_MULTIPLIER = 3.0
ORB_CANDLE_MINUTES = 15          # First 15-min candle for ORB
VWAP_DEVIATION_THRESHOLD = 0.5   # % above/below VWAP

# ── Instruments (NSE Equity) ─────────────────────────────────────────
WATCHLIST = [
    "NSE:RELIANCE",
    "NSE:TCS",
    "NSE:INFY",
    "NSE:HDFCBANK",
    "NSE:ICICIBANK",
    "NSE:SBIN",
    "NSE:BHARTIARTL",
    "NSE:ITC",
    "NSE:KOTAKBANK",
    "NSE:LT",
]

# ── Order Settings ───────────────────────────────────────────────────
ORDER_TYPE = "MARKET"            # MARKET or LIMIT
PRODUCT_TYPE = "MIS"             # MIS for intraday
EXCHANGE = "NSE"
SLIPPAGE_PCT = 0.1               # Assumed slippage for limit orders

# ── Stop Loss & Target ──────────────────────────────────────────────
SL_PCT = 0.5                     # 0.5% stop loss
TARGET_PCT = 1.0                 # 1% target (RR = 1:2)
TRAILING_SL = True               # Enable trailing stop loss
TRAILING_SL_PCT = 0.3            # Trail by 0.3%

# ── Candle Interval ──────────────────────────────────────────────────
CANDLE_INTERVAL_MINUTES = 5      # Default candle size for strategy eval
MAX_CANDLES_IN_MEMORY = 200      # Keep last N candles per instrument

# ── Telegram ─────────────────────────────────────────────────────────
TELEGRAM_BOT_TOKEN = os.getenv("TELEGRAM_BOT_TOKEN", "")
TELEGRAM_CHAT_ID = os.getenv("TELEGRAM_CHAT_ID", "")

# ── Database ─────────────────────────────────────────────────────────
DATABASE_URL = os.getenv("DATABASE_URL", "sqlite:///trades.db")

# ── Logging ──────────────────────────────────────────────────────────
LOG_LEVEL = os.getenv("LOG_LEVEL", "INFO")
LOG_FILE = os.getenv("LOG_FILE", "logs/trading_bot.log")

# ── Session / Token cache ────────────────────────────────────────────
ACCESS_TOKEN_FILE = os.getenv("ACCESS_TOKEN_FILE", "access_token.txt")

# ── Resilience ───────────────────────────────────────────────────────
API_RETRY_COUNT = 3
API_RETRY_BACKOFF = 2            # seconds (exponential)
HEARTBEAT_INTERVAL = 300         # seconds (5 min)
POSITION_SYNC_INTERVAL = 30      # seconds

# ── Consecutive-loss pause ───────────────────────────────────────────
CONSECUTIVE_LOSS_PAUSE_THRESHOLD = 3
CONSECUTIVE_LOSS_PAUSE_MINUTES = 30

# ── Charges (Zerodha Intraday, 2025) ─────────────────────────────────
BROKERAGE_PER_ORDER = 20         # ₹20 per executed order
BROKERAGE_PCT = 0.03             # 0.03% — whichever is lower
STT_PCT = 0.025                  # 0.025% on sell side
TRANSACTION_CHARGES_PCT = 0.00345  # NSE transaction charges
GST_PCT = 18.0                   # 18% on brokerage + txn charges
SEBI_CHARGES_PER_CRORE = 10      # ₹10 per crore
STAMP_DUTY_PCT = 0.003           # 0.003% on buy side


# ── GUI Override ─────────────────────────────────────────────────────
# If the GUI wrote a settings_override.json, apply those values on top.
def _apply_gui_overrides() -> None:
    import json as _json
    override_path = os.path.join(os.path.dirname(__file__), "settings_override.json")
    if not os.path.exists(override_path):
        return
    try:
        with open(override_path) as fh:
            overrides = _json.load(fh)
        g = globals()
        for key, value in overrides.items():
            if key in g:
                g[key] = value
    except Exception:
        pass

_apply_gui_overrides()
