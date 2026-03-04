# Kite Intraday Trading Bot

A production-grade Python intraday trading bot for the Indian stock market (NSE/BSE) using Zerodha's Kite Connect API.

## Features

- **Authentication** — Kite Connect OAuth2 login with daily token caching
- **Real-time data** — WebSocket live tick streaming with candle aggregation
- **4 Strategies** — EMA Crossover, Opening Range Breakout, VWAP Breakout, Supertrend
- **Multi-strategy confluence** — Trade only when 2+ strategies agree
- **Order management** — Entry, stop-loss, target, and trailing stop-loss orders
- **Risk management** — Position sizing, daily loss limits, consecutive-loss pause, max positions
- **Position tracking** — Real-time P&L with Kite reconciliation
- **Auto square-off** — All positions closed 20 minutes before market close
- **Trade logging** — SQLite/PostgreSQL persistence via SQLAlchemy
- **Notifications** — Telegram alerts for entries, exits, errors, and daily summaries
- **Backtesting** — Historical replay engine with performance metrics

## Project Structure

```
├── config/
│   ├── .env.example        # Template for API keys
│   └── settings.py         # All tunable parameters
├── core/
│   ├── auth.py             # Kite login + token management
│   ├── data_feed.py        # WebSocket ticker + candle builder
│   ├── strategy.py         # Base strategy + confluence engine
│   ├── order_manager.py    # Place, modify, cancel orders
│   ├── risk_manager.py     # Position sizing, circuit breakers
│   └── position_tracker.py # Track positions and P&L
├── strategies/
│   ├── ema_crossover.py    # EMA 9/21 crossover
│   ├── orb.py              # Opening Range Breakout
│   ├── vwap_breakout.py    # VWAP-based breakout
│   └── supertrend.py       # Supertrend indicator
├── backtest/
│   ├── backtester.py       # Historical backtesting engine
│   └── data_downloader.py  # Fetch candles from Kite
├── utils/
│   ├── logger.py           # Rotating file logging
│   ├── notifier.py         # Telegram alerts
│   ├── helpers.py          # Time checks, charges, retry decorator
│   └── db.py               # SQLAlchemy models + CRUD
├── main.py                 # Entry point
├── requirements.txt
└── README.md
```

## Quick Start

### 1. Install dependencies

```bash
python -m venv venv
source venv/bin/activate
pip install -r requirements.txt
```

### 2. Configure

```bash
cp config/.env.example config/.env
```

Edit `config/.env` with your Kite Connect API key, secret, and optional Telegram credentials.

### 3. Run

```bash
python main.py
```

On first run, you'll be prompted to log in via the Kite Connect URL. Paste the `request_token` to authenticate. The access token is cached for the day.

## Configuration

All parameters are in `config/settings.py`:

| Parameter | Default | Description |
|---|---|---|
| `TOTAL_CAPITAL` | ₹1,00,000 | Capital allocated |
| `RISK_PER_TRADE_PCT` | 1% | Risk per trade |
| `MAX_TRADES_PER_DAY` | 5 | Daily trade limit |
| `MAX_DAILY_LOSS_PCT` | 3% | Stop trading threshold |
| `SL_PCT` | 0.5% | Stop-loss percentage |
| `TARGET_PCT` | 1.0% | Target percentage (1:2 RR) |
| `SQUARE_OFF_TIME` | 15:10 | Auto square-off time |
| `CANDLE_INTERVAL_MINUTES` | 5 | Candle size for strategies |

## Strategies

1. **EMA Crossover** — EMA(9)/EMA(21) crossover with VWAP + RSI + volume confirmation
2. **Opening Range Breakout** — First 15-min candle high/low breakout
3. **VWAP Breakout** — 3-candle sustained VWAP cross with RSI
4. **Supertrend** — Supertrend(10,3) direction changes

The bot uses **multi-strategy confluence** — a trade is only placed when at least 2 strategies agree on direction.

## Backtesting

```python
from backtest.backtester import Backtester
from backtest.data_downloader import download_historical
from strategies.ema_crossover import EMACrossoverStrategy

# Download data
df = download_historical(kite, token, "RELIANCE", from_date, to_date)

# Run backtest
bt = Backtester(EMACrossoverStrategy, df, instrument="RELIANCE")
result = bt.run()
bt.print_summary(result)
```

## Deployment

Recommended: Ubuntu 22.04 VPS in Mumbai region (AWS `ap-south-1`).

```ini
# /etc/systemd/system/kite-bot.service
[Unit]
Description=Kite Intraday Trading Bot
After=network.target

[Service]
Type=simple
User=traderbot
WorkingDirectory=/home/traderbot/kite-intraday-bot
ExecStart=/home/traderbot/venv/bin/python main.py
Restart=on-failure
RestartSec=10
Environment=PYTHONUNBUFFERED=1

[Install]
WantedBy=multi-user.target
```

## Disclaimer

**This is for educational purposes only.** Automated trading involves significant financial risk. No guaranteed profits. Paper trade extensively before using real capital. You are solely responsible for any financial losses. Ensure compliance with Zerodha's Terms of Service and SEBI regulations.
