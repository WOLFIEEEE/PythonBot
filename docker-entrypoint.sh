#!/bin/bash
set -e

echo "=============================================="
echo " Kite Intraday Trading Bot — Docker Startup"
echo "=============================================="

# ── 1. Verify required environment variables ──────────────────────────
if [ -z "$KITE_API_KEY" ] || [ -z "$KITE_API_SECRET" ]; then
    echo ""
    echo "ERROR: KITE_API_KEY and KITE_API_SECRET must be set."
    echo ""
    echo "Create a config/.env file with:"
    echo "  KITE_API_KEY=your_api_key"
    echo "  KITE_API_SECRET=your_api_secret"
    echo ""
    echo "Or pass them via docker-compose environment variables."
    exit 1
fi

echo "[OK] Kite API credentials found."

# ── 2. Ensure data directories exist ─────────────────────────────────
mkdir -p /app/data /app/logs

echo "[OK] Data directories ready."

# ── 3. Initialize SQLite database ────────────────────────────────────
echo "[..] Initializing database at ${DATABASE_URL:-sqlite:////app/data/trades.db}..."
python -c "
from utils.db import init_db
init_db()
print('[OK] Database initialized successfully.')
"

# ── 4. Verify timezone ───────────────────────────────────────────────
echo "[OK] Timezone: $(date +%Z) ($(date))"

# ── 5. Show config summary ──────────────────────────────────────────
python -c "
from config import settings
print(f'[OK] Capital: INR {settings.TOTAL_CAPITAL:,.0f}')
print(f'[OK] Risk per trade: {settings.RISK_PER_TRADE_PCT}%')
print(f'[OK] Max trades/day: {settings.MAX_TRADES_PER_DAY}')
print(f'[OK] Max daily loss: {settings.MAX_DAILY_LOSS_PCT}%')
print(f'[OK] Square-off time: {settings.SQUARE_OFF_TIME}')
print(f'[OK] Watchlist: {len(settings.WATCHLIST)} instruments')
telegram = 'Configured' if settings.TELEGRAM_BOT_TOKEN else 'Not configured'
print(f'[OK] Telegram alerts: {telegram}')
"

echo ""
echo "Starting trading bot..."
echo "=============================================="

# ── 6. Run the command (default: python main.py) ─────────────────────
exec "$@"
