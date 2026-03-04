# Kite Intraday Trading Bot

A production-grade, fully automated Python intraday trading bot for the Indian stock market (NSE/BSE) using **Zerodha's Kite Connect API**. The bot handles authentication, real-time WebSocket data streaming, multi-strategy signal generation, order management, risk controls, position tracking, and automatic end-of-day square-off — all within market hours (9:15 AM – 3:30 PM IST).

---

## Table of Contents

- [Features](#features)
- [Architecture Overview](#architecture-overview)
- [Project Structure](#project-structure)
- [Prerequisites](#prerequisites)
- [Installation](#installation)
- [Configuration](#configuration)
  - [Environment Variables](#environment-variables)
  - [Trading Parameters](#trading-parameters)
  - [Watchlist](#watchlist)
  - [Strategy Parameters](#strategy-parameters)
- [Authentication Flow](#authentication-flow)
- [How the Bot Works](#how-the-bot-works)
  - [Startup Sequence](#startup-sequence)
  - [Main Trading Loop](#main-trading-loop)
  - [Order Lifecycle](#order-lifecycle)
  - [Shutdown Sequence](#shutdown-sequence)
- [Trading Strategies](#trading-strategies)
  - [EMA Crossover](#1-ema-crossover)
  - [Opening Range Breakout (ORB)](#2-opening-range-breakout-orb)
  - [VWAP Breakout](#3-vwap-breakout)
  - [Supertrend](#4-supertrend)
  - [Multi-Strategy Confluence](#multi-strategy-confluence)
- [Risk Management](#risk-management)
  - [Position Sizing](#position-sizing)
  - [Pre-Trade Checks](#pre-trade-checks)
  - [Circuit Breakers](#circuit-breakers)
- [Order Management](#order-management)
- [Real-Time Data Feed](#real-time-data-feed)
- [Position Tracking](#position-tracking)
- [Trade Logging & Database](#trade-logging--database)
- [Telegram Notifications](#telegram-notifications)
- [Backtesting](#backtesting)
- [Charges & Tax Estimation](#charges--tax-estimation)
- [Error Handling & Resilience](#error-handling--resilience)
- [Deployment](#deployment)
  - [VPS Setup](#vps-setup)
  - [systemd Service](#systemd-service)
  - [Cron Jobs](#cron-jobs)
- [Security Best Practices](#security-best-practices)
- [Testing Checklist](#testing-checklist)
- [Troubleshooting](#troubleshooting)
- [Disclaimer](#disclaimer)
- [License](#license)

---

## Features

| Feature | Description |
|---|---|
| **Kite Connect Auth** | OAuth2 login with daily access token caching — no re-auth within the same day |
| **Live Data Streaming** | WebSocket (KiteTicker) in `MODE_FULL` with real-time OHLCV candle aggregation |
| **4 Built-in Strategies** | EMA Crossover, Opening Range Breakout, VWAP Breakout, Supertrend |
| **Multi-Strategy Confluence** | Only trades when 2+ strategies agree on direction — reduces false signals |
| **Full Order Lifecycle** | Entry, stop-loss, target, trailing stop-loss, and auto square-off orders |
| **7-Point Risk Checks** | Daily loss limit, max trades, max positions, time gate, margin, duplicates, loss pause |
| **Position Tracking** | In-memory tracking with periodic Kite API reconciliation and real-time P&L |
| **Auto Square-Off** | All MIS positions closed at 15:10 IST (20 min before market close) |
| **Trade Logging** | Every trade persisted to SQLite/PostgreSQL via SQLAlchemy |
| **Telegram Alerts** | Instant notifications for entries, exits, errors, risk breaches, daily summary |
| **Backtesting Engine** | Historical replay with Sharpe ratio, max drawdown, profit factor, win rate |
| **Charges Estimation** | Brokerage, STT, GST, SEBI charges, stamp duty — accurate net P&L |
| **Graceful Shutdown** | SIGINT/SIGTERM handlers square off positions before exiting |
| **Resilience** | Retry with exponential backoff on all API calls, WebSocket auto-reconnect, session heartbeat |

---

## Architecture Overview

```
┌──────────────┐     ┌──────────────┐     ┌──────────────────┐
│   Kite API   │────>│   auth.py    │────>│  KiteConnect     │
│  (Zerodha)   │     │  OAuth2 +    │     │  Instance        │
│              │     │  Token Cache │     │  (authenticated) │
└──────┬───────┘     └──────────────┘     └────────┬─────────┘
       │                                           │
       v                                           v
┌──────────────┐     ┌──────────────┐     ┌──────────────────┐
│  KiteTicker  │────>│ data_feed.py │────>│ CandleAggregator │
│  WebSocket   │     │  Live Ticks  │     │ Ticks -> OHLCV   │
└──────────────┘     └──────────────┘     └────────┬─────────┘
                                                   │ on_candle()
                                                   v
                                          ┌──────────────────┐
                                          │  strategy.py     │
                                          │  StrategyEngine  │
                                          │  (Confluence)    │
                                          └────────┬─────────┘
                                                   │ BUY/SELL/HOLD
                                                   v
┌──────────────┐     ┌──────────────┐     ┌──────────────────┐
│ risk_manager │<────│   main.py    │────>│ order_manager.py │
│ Pre-trade    │     │ Orchestrator │     │ Place/Modify/    │
│ Checks       │     │              │     │ Cancel Orders    │
└──────────────┘     └──────┬───────┘     └──────────────────┘
                            │
              ┌─────────────┼─────────────┐
              v             v             v
     ┌──────────────┐ ┌──────────┐ ┌──────────────┐
     │position_     │ │  db.py   │ │ notifier.py  │
     │tracker.py    │ │ SQLAlch. │ │ Telegram     │
     │ Live P&L     │ │ Trade Log│ │ Alerts       │
     └──────────────┘ └──────────┘ └──────────────┘
```

---

## Project Structure

```
kite-intraday-bot/
├── config/
│   ├── __init__.py
│   ├── .env.example          # Template — copy to .env and fill in secrets
│   └── settings.py           # All tunable parameters (capital, risk, timing, etc.)
│
├── core/
│   ├── __init__.py
│   ├── auth.py               # Kite Connect OAuth2 login + daily token caching
│   ├── data_feed.py          # KiteTicker WebSocket + real-time candle aggregation
│   ├── strategy.py           # BaseStrategy ABC + StrategyEngine confluence
│   ├── order_manager.py      # Place, modify, cancel, square-off orders
│   ├── risk_manager.py       # Position sizing, 7 pre-trade checks, circuit breakers
│   └── position_tracker.py   # In-memory position tracking + Kite reconciliation
│
├── strategies/
│   ├── __init__.py
│   ├── ema_crossover.py      # EMA(9)/EMA(21) crossover with VWAP + RSI + volume
│   ├── orb.py                # Opening Range Breakout (first 15-min candle)
│   ├── vwap_breakout.py      # VWAP sustained breakout with RSI confirmation
│   └── supertrend.py         # Supertrend(10, 3) direction-change signals
│
├── backtest/
│   ├── __init__.py
│   ├── backtester.py         # Historical replay engine with performance metrics
│   └── data_downloader.py    # Fetch OHLCV candles from Kite historical API
│
├── utils/
│   ├── __init__.py
│   ├── logger.py             # Centralized logging with rotating file handlers
│   ├── notifier.py           # Telegram Bot API notifications
│   ├── helpers.py            # Time checks, instrument lookup, charges calc, retry
│   └── db.py                 # SQLAlchemy models (TradeLog, DailySummary) + CRUD
│
├── main.py                   # Entry point — full orchestration
├── requirements.txt          # Python dependencies
├── .gitignore                # Excludes secrets, tokens, databases, logs
└── README.md                 # This file
```

---

## Prerequisites

Before you begin, make sure you have:

1. **Python 3.10+** installed on your system
2. **Zerodha Kite Connect API subscription** — sign up at [Kite Connect](https://developers.kite.trade/)
   - You'll receive an `api_key` and `api_secret`
   - Annual subscription fee applies (check Zerodha's pricing)
3. **Zerodha trading account** with sufficient capital for MIS (intraday) trades
4. **Telegram Bot** (optional but recommended) — for real-time alerts
   - Create a bot via [@BotFather](https://t.me/BotFather) on Telegram
   - Get your `chat_id` by messaging [@userinfobot](https://t.me/userinfobot)
5. **A VPS** (recommended for production) — Ubuntu 22.04 in Mumbai region for low latency

---

## Installation

### Step 1: Clone the Repository

```bash
git clone https://github.com/WOLFIEEEE/PythonBot.git
cd PythonBot
```

### Step 2: Create a Virtual Environment

```bash
python3 -m venv venv
source venv/bin/activate        # Linux/macOS
# OR
venv\Scripts\activate           # Windows
```

### Step 3: Install Dependencies

```bash
pip install -r requirements.txt
```

The following packages will be installed:

| Package | Purpose |
|---|---|
| `kiteconnect>=5.0.0` | Zerodha Kite Connect SDK (API + WebSocket) |
| `pandas>=2.0.0` | DataFrame operations for candle data and indicators |
| `numpy>=1.24.0` | Numerical computations for strategies |
| `python-dotenv>=1.0.0` | Load secrets from `.env` file |
| `apscheduler>=3.10.0` | Schedule periodic tasks (heartbeat, square-off, summary) |
| `requests>=2.31.0` | HTTP client for Telegram notifications |
| `sqlalchemy>=2.0.0` | ORM for trade logging to SQLite/PostgreSQL |
| `pytz>=2023.3` | IST timezone handling |
| `websocket-client>=1.6.0` | WebSocket dependency for KiteTicker |

### Step 4: Configure Environment

```bash
cp config/.env.example config/.env
```

Edit `config/.env` with your actual credentials (see next section).

### Step 5: Create Log Directory

```bash
mkdir -p logs
```

---

## Configuration

### Environment Variables

Edit `config/.env` with your credentials:

```env
# REQUIRED — Kite Connect API credentials
KITE_API_KEY=your_api_key_here
KITE_API_SECRET=your_api_secret_here

# OPTIONAL — Telegram notifications
TELEGRAM_BOT_TOKEN=your_telegram_bot_token
TELEGRAM_CHAT_ID=your_chat_id

# OPTIONAL — Database (defaults to SQLite)
DATABASE_URL=sqlite:///trades.db
# For PostgreSQL: DATABASE_URL=postgresql://user:pass@localhost:5432/trades

# OPTIONAL — Logging
LOG_LEVEL=INFO
LOG_FILE=logs/trading_bot.log
```

**Important:** Never commit `config/.env` to version control. It is already in `.gitignore`.

### Trading Parameters

All tunable parameters are in `config/settings.py`. Here are the key ones:

#### Capital & Risk

| Parameter | Default | Description |
|---|---|---|
| `TOTAL_CAPITAL` | `100,000` | Total capital allocated in INR |
| `RISK_PER_TRADE_PCT` | `1.0` | Maximum risk per trade as % of capital |
| `MAX_TRADES_PER_DAY` | `5` | Maximum number of trades per session |
| `MAX_DAILY_LOSS_PCT` | `3.0` | Stop all trading if daily loss exceeds this % |
| `MAX_OPEN_POSITIONS` | `3` | Maximum concurrent open positions |

#### Market Timing (IST)

| Parameter | Default | Description |
|---|---|---|
| `MARKET_OPEN` | `09:15` | NSE market open time |
| `MARKET_CLOSE` | `15:30` | NSE market close time |
| `SQUARE_OFF_TIME` | `15:10` | Auto square-off all positions (20 min before close) |
| `NO_NEW_TRADES_AFTER` | `14:30` | No new entries allowed after this time |

#### Stop-Loss & Target

| Parameter | Default | Description |
|---|---|---|
| `SL_PCT` | `0.5` | Stop-loss as % of entry price |
| `TARGET_PCT` | `1.0` | Target as % of entry price (gives 1:2 RR) |
| `TRAILING_SL` | `True` | Enable trailing stop-loss |
| `TRAILING_SL_PCT` | `0.3` | Trail by this % as price moves in favour |

#### Order Settings

| Parameter | Default | Description |
|---|---|---|
| `ORDER_TYPE` | `MARKET` | `MARKET` or `LIMIT` |
| `PRODUCT_TYPE` | `MIS` | `MIS` for intraday (auto square-off by broker at 3:20 PM) |
| `EXCHANGE` | `NSE` | Exchange to trade on |
| `SLIPPAGE_PCT` | `0.1` | Assumed slippage for backtesting |

#### Candle & Data

| Parameter | Default | Description |
|---|---|---|
| `CANDLE_INTERVAL_MINUTES` | `5` | Candle timeframe for strategy evaluation |
| `MAX_CANDLES_IN_MEMORY` | `200` | Keep last N candles per instrument in memory |

#### Resilience

| Parameter | Default | Description |
|---|---|---|
| `API_RETRY_COUNT` | `3` | Max retries for failed API calls |
| `API_RETRY_BACKOFF` | `2` | Exponential backoff base (seconds) |
| `HEARTBEAT_INTERVAL` | `300` | Session verification interval (5 min) |
| `POSITION_SYNC_INTERVAL` | `30` | Position reconciliation interval (30 sec) |
| `CONSECUTIVE_LOSS_PAUSE_THRESHOLD` | `3` | Pause after N consecutive losses |
| `CONSECUTIVE_LOSS_PAUSE_MINUTES` | `30` | Pause duration after consecutive losses |

### Watchlist

Edit the `WATCHLIST` in `config/settings.py` to change which instruments are monitored:

```python
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
```

Format: `"EXCHANGE:TRADINGSYMBOL"` — The bot will resolve these to instrument tokens automatically.

### Strategy Parameters

| Parameter | Default | Used By |
|---|---|---|
| `EMA_FAST` | `9` | EMA Crossover |
| `EMA_SLOW` | `21` | EMA Crossover |
| `SUPERTREND_PERIOD` | `10` | Supertrend |
| `SUPERTREND_MULTIPLIER` | `3.0` | Supertrend |
| `ORB_CANDLE_MINUTES` | `15` | Opening Range Breakout |
| `VWAP_DEVIATION_THRESHOLD` | `0.5` | VWAP Breakout |

---

## Authentication Flow

Kite Connect uses OAuth2. The bot handles this automatically:

```
1. Bot starts -> loads api_key, api_secret from config/.env
2. Checks for today's cached token in access_token.txt
   |-- Found -> sets token -> verifies with kite.profile()
   |   |-- Valid -> proceed (no browser needed)
   |   |-- Invalid -> fall through to manual login
   |-- Not found -> manual login
3. Manual login:
   a. Bot prints a login URL to the console
   b. You open it in your browser -> log in to Zerodha
   c. After login, Zerodha redirects with a request_token in the URL
   d. You paste the request_token into the console
   e. Bot exchanges it for an access_token via kite.generate_session()
   f. Token is cached to access_token.txt with today's date
4. Authenticated KiteConnect instance is ready
```

**Note:** Zerodha requires manual browser login each day. The token is valid for one trading day only. The bot caches it so you only need to log in once per day.

**On subsequent runs the same day:** The bot will automatically use the cached token — no browser interaction needed.

---

## How the Bot Works

### Startup Sequence

When you run `python main.py`, the bot executes the following steps in order:

```
1.  Initialise database (create tables if not exist)
2.  Authenticate with Kite Connect -> get authenticated kite instance
3.  Fetch full NSE instrument list -> map tradingsymbol -> instrument_token
4.  Resolve watchlist symbols to instrument tokens
5.  Initialise OrderManager, RiskManager, PositionTracker
6.  Initialise StrategyEngine with all 4 strategies (min_agreement=2)
7.  Start WebSocket data feed -> subscribe to watchlist tokens in MODE_FULL
8.  Start APScheduler with periodic jobs:
    - Heartbeat every 5 minutes (verify session)
    - Position sync every 30 seconds (reconcile with Kite)
    - Square-off at 15:10 IST
    - Daily summary at 15:30 IST
9.  Register SIGINT/SIGTERM handlers for graceful shutdown
10. Send Telegram notification: "Bot STARTED"
11. Main thread waits (event loop)
```

### Main Trading Loop

The trading loop is **event-driven**, triggered by the WebSocket data feed:

```
Every time a candle closes (e.g., every 5 minutes):
  For each instrument in the watchlist:
    1. Feed the latest candle DataFrame to the StrategyEngine
    2. All 4 strategies compute indicators and generate signals
    3. StrategyEngine checks confluence -- need 2+ strategies to agree
    4. If signal is BUY or SELL:
       a. RiskManager runs 7 pre-trade checks
       b. If all pass -> calculate position size
       c. Place entry order via OrderManager
       d. Wait for fill (poll order status, timeout 30s)
       e. On fill -> place SL order + target order
       f. Open position in PositionTracker
       g. Send Telegram notification
    5. For existing open positions:
       a. Check if SL order filled -> close position, cancel target
       b. Check if target order filled -> close position, cancel SL
       c. Update trailing stop-loss if enabled
       d. Update unrealised P&L from live prices
```

### Order Lifecycle

```
Signal Generated
    |
    v
Pre-trade Checks ---- FAIL --> Skip (log reason)
    |
    PASS
    |
    v
Place Entry Order (MARKET/LIMIT)
    |
    v
Wait for Fill (poll up to 30s)
    |
    |-- COMPLETE --> Place SL Order + Target Order
    |                    |
    |                    v
    |               Monitor Position:
    |                 - SL hit? -> cancel target, close position
    |                 - Target hit? -> cancel SL, close position
    |                 - Price moved? -> update trailing SL
    |                 - Square-off time? -> market close everything
    |
    |-- REJECTED --> Log error, send Telegram alert
    |
    |-- TIMEOUT ---> Cancel order, skip
```

### Shutdown Sequence

Triggered by `Ctrl+C` (SIGINT), `kill` (SIGTERM), or at `SQUARE_OFF_TIME`:

```
1. Set shutdown event flag
2. Cancel all pending (OPEN / TRIGGER PENDING) orders
3. Square off all open MIS positions with market orders
4. Close all positions in tracker, calculate final P&L
5. Persist daily summary to database
6. Send Telegram daily summary
7. Stop WebSocket connection
8. Stop APScheduler
9. Send Telegram notification: "Bot STOPPED"
10. Exit
```

---

## Trading Strategies

### 1. EMA Crossover

**File:** `strategies/ema_crossover.py`

Uses Exponential Moving Average crossover with multiple confirmations:

| Condition | BUY Signal | SELL Signal |
|---|---|---|
| EMA Crossover | EMA(9) crosses **above** EMA(21) | EMA(9) crosses **below** EMA(21) |
| VWAP | Close **above** VWAP | Close **below** VWAP |
| Volume | Current volume > 1.5x 20-period average | -- |
| RSI(14) | Between 40 and 70 (not overbought) | Between 30 and 60 (not oversold) |

**Why multiple confirmations?** Reduces false signals from EMA crossovers in choppy/sideways markets.

### 2. Opening Range Breakout (ORB)

**File:** `strategies/orb.py`

Captures the first 15-minute candle range (9:15-9:30 AM) and trades the breakout:

- **BUY:** Price breaks above the ORB high with volume > 1.2x average
- **SELL:** Price breaks below the ORB low with volume > 1.2x average
- **Stop-loss:** Opposite end of the ORB range
- Best suited for volatile stocks with strong opening moves

### 3. VWAP Breakout

**File:** `strategies/vwap_breakout.py`

Volume Weighted Average Price (VWAP) is the institutional benchmark:

- **BUY:** Price sustains **above** VWAP for 3 consecutive candles + RSI > 50
- **SELL:** Price sustains **below** VWAP for 3 consecutive candles + RSI < 50
- VWAP calculated as cumulative(TypicalPrice x Volume) / cumulative(Volume)

### 4. Supertrend

**File:** `strategies/supertrend.py`

Trend-following indicator based on ATR (Average True Range):

- **BUY:** Supertrend direction changes from -1 (down) to +1 (up)
- **SELL:** Supertrend direction changes from +1 (up) to -1 (down)
- Default parameters: Period=10, Multiplier=3.0
- Uses ATR for dynamic stop-loss levels

### Multi-Strategy Confluence

**File:** `core/strategy.py` — `StrategyEngine` class

The bot runs **all 4 strategies simultaneously** on each candle and only places a trade when at least **2 out of 4** strategies agree on direction:

```python
strategy_engine = StrategyEngine(
    strategy_classes=[
        EMACrossoverStrategy,
        SupertrendStrategy,
        VWAPBreakoutStrategy,
        ORBStrategy,
    ],
    min_agreement=2,  # At least 2 must agree
)
```

**Why confluence?** Individual strategies can generate false signals. Requiring agreement from multiple independent strategies dramatically reduces bad trades.

You can change `min_agreement` to `3` for even stricter filtering, or `1` for more aggressive trading.

---

## Risk Management

### Position Sizing

Position size is calculated using a **fixed-risk model** — never risking more than `RISK_PER_TRADE_PCT` (default 1%) of capital on any single trade:

```
risk_amount = capital x (risk_pct / 100)
risk_per_share = |entry_price - stop_loss_price|
quantity = risk_amount / risk_per_share
```

**Example:** With INR 1,00,000 capital, 1% risk, entry at 2,450, SL at 2,438:
- Risk amount = INR 1,000
- Risk per share = INR 12
- Quantity = 83 shares

### Pre-Trade Checks

**Every trade must pass all 7 checks** before an order is placed:

| # | Check | Blocks If |
|---|---|---|
| 1 | Daily loss limit | Cumulative P&L loss > `MAX_DAILY_LOSS_PCT` (3%) of capital |
| 2 | Max trades per day | Already placed `MAX_TRADES_PER_DAY` (5) trades today |
| 3 | Max open positions | Already have `MAX_OPEN_POSITIONS` (3) positions open |
| 4 | Time gate | Current time > `NO_NEW_TRADES_AFTER` (14:30) |
| 5 | Margin check | Insufficient available margin in Kite account |
| 6 | No duplicates | Already holding a position in this instrument |
| 7 | Consecutive-loss pause | 3+ consecutive losses -> paused for 30 minutes |

### Circuit Breakers

| Circuit Breaker | Trigger | Action |
|---|---|---|
| **Daily loss limit** | Realized + unrealized loss > 3% of capital | Square off all positions, stop trading |
| **Consecutive losses** | 3 consecutive losing trades | Pause new trades for 30 minutes |
| **Time cutoff** | After 14:30 IST | No new entries (but existing positions managed) |
| **Auto square-off** | At 15:10 IST | Close all open positions, cancel pending orders |

---

## Order Management

**File:** `core/order_manager.py`

Supported order types:

| Order Type | Usage | Kite order_type |
|---|---|---|
| Entry | Open a position | `MARKET` or `LIMIT` (configurable) |
| Stop-Loss | Protect against loss | `SL-M` (stop-loss market) |
| Target | Lock in profit | `LIMIT` |
| Trailing Stop-Loss | Dynamic SL modification | Modifies existing SL order |
| Square-Off | Close position at EOD | `MARKET` |

All orders use:
- `variety="regular"`
- `product="MIS"` (intraday — broker auto-closes at 3:20 PM as fallback)
- `exchange="NSE"`

**Trailing Stop-Loss:** When enabled, the bot periodically checks if the price has moved in favour. If so, the SL order is modified upward (for longs) or downward (for shorts) by `TRAILING_SL_PCT` (0.3%).

**Error Handling:** All order calls use retry with exponential backoff. Order rejections and failures trigger Telegram alerts.

---

## Real-Time Data Feed

**File:** `core/data_feed.py`

The data feed consists of two components:

### KiteTicker WebSocket
- Connects to Zerodha's streaming API using the authenticated access token
- Subscribes to all instruments in the watchlist using `MODE_FULL` (OHLC, volume, bid/ask, OI)
- Handles reconnection, errors, and dropped connections automatically
- Runs in a background daemon thread

### CandleAggregator
Converts raw ticks into OHLCV candles in real time:

```
For each incoming tick:
  1. Floor the timestamp to the candle interval boundary (e.g., 5 min)
  2. If same interval as current candle -> update High, Low, Close, Volume
  3. If new interval -> finalize current candle, start new candle
  4. When a candle is finalized -> trigger strategy evaluation callback
```

The aggregator keeps the last `MAX_CANDLES_IN_MEMORY` (200) candles per instrument as a pandas DataFrame.

---

## Position Tracking

**File:** `core/position_tracker.py`

Maintains an in-memory dictionary of all open positions:

```python
{
    "RELIANCE": Position(
        instrument="RELIANCE",
        direction="BUY",
        entry_price=2450.50,
        quantity=40,
        sl_order_id="230901000012345",
        target_order_id="230901000012346",
        entry_time=datetime(2025, 3, 4, 9, 45),
        current_pnl=320.00,
        trailing_sl=2438.50,
        strategy="EMA Crossover",
    )
}
```

**Kite Reconciliation:** Every 30 seconds, the tracker syncs with `kite.positions()` to detect externally closed positions (e.g., manual intervention, broker square-off).

**P&L Tracking:**
- **Unrealised P&L:** Updated in real-time from live tick prices
- **Realised P&L:** Calculated when a position is closed (gross P&L minus charges)
- **Max Drawdown:** Tracked across all closed trades for daily summary

---

## Trade Logging & Database

**File:** `utils/db.py`

Uses SQLAlchemy ORM with two tables:

### TradeLog Table

| Column | Type | Description |
|---|---|---|
| `id` | Integer | Primary key |
| `date` | Date | Trade date |
| `instrument` | String | Tradingsymbol (e.g., RELIANCE) |
| `direction` | String | BUY or SELL |
| `entry_price` | Float | Entry fill price |
| `exit_price` | Float | Exit fill price |
| `quantity` | Integer | Number of shares |
| `entry_time` | DateTime | Entry timestamp |
| `exit_time` | DateTime | Exit timestamp |
| `pnl` | Float | Net P&L (after charges) |
| `strategy` | String | Strategy that generated the signal |
| `sl_price` | Float | Stop-loss price |
| `target_price` | Float | Target price |
| `exit_reason` | String | `SL_HIT`, `TARGET_HIT`, `SQUARE_OFF`, or `MANUAL` |
| `order_ids` | String | JSON array of related order IDs |
| `charges` | Float | Estimated brokerage + taxes |

### DailySummary Table

| Column | Type | Description |
|---|---|---|
| `date` | Date | Trading date (unique) |
| `total_trades` | Integer | Total trades executed |
| `winning_trades` | Integer | Trades with positive P&L |
| `losing_trades` | Integer | Trades with negative P&L |
| `gross_pnl` | Float | Gross P&L before charges |
| `net_pnl` | Float | Net P&L after charges |
| `max_drawdown` | Float | Maximum intraday drawdown |
| `capital_used` | Float | Total capital deployed |

**Database choice:**
- Default: **SQLite** (`sqlite:///trades.db`) — zero setup, good for development
- Production: Set `DATABASE_URL=postgresql://user:pass@host:5432/trades` in `.env`

---

## Telegram Notifications

**File:** `utils/notifier.py`

The bot sends Telegram alerts for all significant events.

### Setup Steps

1. Open Telegram and search for **@BotFather**
2. Send `/newbot` and follow the prompts to create a bot
3. Copy the **bot token** (looks like `123456:ABC-DEF1234ghIkl-zyx57W2v1u123ew11`)
4. Message your bot, then visit `https://api.telegram.org/bot<TOKEN>/getUpdates` to find your **chat_id**
5. Add both to `config/.env`:
   ```env
   TELEGRAM_BOT_TOKEN=123456:ABC-DEF1234ghIkl-zyx57W2v1u123ew11
   TELEGRAM_CHAT_ID=987654321
   ```

### Notification Examples

**Trade Entry:**
```
BUY RELIANCE @ Rs.2,450.50
Qty: 40 | SL: Rs.2,438.25 | Target: Rs.2,475.00
Strategy: EMA Crossover | Risk: Rs.490
```

**Stop-Loss Hit:**
```
SL HIT -- RELIANCE
Exit: Rs.2,438.00 | P&L: -Rs.500
Duration: 22 min
```

**Target Hit:**
```
TARGET HIT -- RELIANCE
Exit: Rs.2,475.00 | P&L: +Rs.980
Duration: 45 min
```

**Daily Summary (at 3:30 PM):**
```
Daily Summary -- 04-Mar-2025
Trades: 4 | Win: 3 | Loss: 1
Gross P&L: +Rs.2,340 | Net: +Rs.2,100
Win Rate: 75% | Max DD: Rs.500
```

**Risk Breach:**
```
RISK BREACH: Daily loss limit breached (P&L=-3100, limit=-3000)
```

**Bot Status:**
```
Bot STARTED / Bot STOPPED
```

If Telegram is not configured, notifications are silently skipped (the bot continues to function normally).

---

## Backtesting

### Download Historical Data

```python
from core.auth import authenticate
from backtest.data_downloader import download_historical
from datetime import date

kite = authenticate()

# Download 5-minute candles for RELIANCE
# Note: Kite limits intraday data to ~60 days
df = download_historical(
    kite=kite,
    instrument_token=738561,         # RELIANCE token
    symbol="RELIANCE",
    from_date=date(2025, 1, 1),
    to_date=date(2025, 2, 28),
    interval="5minute",             # Options: minute, 5minute, 15minute, day
    save_csv=True,                   # Saves to backtest/data/
)
```

### Run a Backtest

```python
from backtest.backtester import Backtester
from strategies.ema_crossover import EMACrossoverStrategy

bt = Backtester(
    strategy_class=EMACrossoverStrategy,
    data=df,
    instrument="RELIANCE",
    capital=100000,
    sl_pct=0.5,       # 0.5% stop loss
    target_pct=1.0,    # 1.0% target
    slippage_pct=0.1,  # 0.1% slippage assumption
)

result = bt.run(lookback=50)  # Need 50 candles of history for indicators
bt.print_summary(result)
```

### Output Example

```
==================================================
  Backtest: EMA Crossover
  Instrument: RELIANCE
  Candles: 2400
==================================================
  total_trades                  : 42
  winning                       : 24
  losing                        : 18
  win_rate                      : 57.1%
  total_pnl                     : 18450.50
  max_drawdown                  : 3200.00
  sharpe_ratio                  : 1.45
  profit_factor                 : 1.82
  avg_trade_duration_candles    : 8.3
==================================================
```

### Metrics Explained

| Metric | Description |
|---|---|
| **Win Rate** | Percentage of trades that were profitable |
| **Total P&L** | Net profit/loss after charges and slippage |
| **Max Drawdown** | Largest peak-to-trough decline in equity |
| **Sharpe Ratio** | Risk-adjusted return (annualised). Above 1.0 is good, above 2.0 is excellent |
| **Profit Factor** | Gross profit / gross loss. Above 1.5 is good |
| **Avg Duration** | Average trade duration in number of candles |

---

## Charges & Tax Estimation

**File:** `utils/helpers.py` — `calculate_charges()` function

The bot estimates Zerodha intraday charges for accurate net P&L:

| Charge | Rate | Applied On |
|---|---|---|
| **Brokerage** | Rs.20 per order OR 0.03% (whichever is lower) | Each executed order |
| **STT** | 0.025% | Sell-side turnover |
| **Transaction Charges** | 0.00345% | Total turnover (NSE) |
| **GST** | 18% | Brokerage + Transaction charges |
| **SEBI Charges** | Rs.10 per crore | Total turnover |
| **Stamp Duty** | 0.003% | Buy-side turnover |

**Example:** Buy RELIANCE at Rs.2,450 x 40 shares, Sell at Rs.2,475:
- Buy turnover: Rs.98,000 | Sell turnover: Rs.99,000
- Brokerage: Rs.40 (Rs.20 x 2)
- STT: Rs.24.75 | Txn charges: Rs.6.80 | GST: Rs.8.42 | Stamp: Rs.2.94
- **Total charges: ~Rs.83**
- Gross P&L: Rs.1,000 | **Net P&L: ~Rs.917**

---

## Error Handling & Resilience

| Mechanism | Implementation |
|---|---|
| **Retry with backoff** | All Kite API calls retry up to 3 times with exponential backoff (2s, 4s, 8s) |
| **Session heartbeat** | `kite.profile()` called every 5 minutes to verify session is alive |
| **WebSocket reconnect** | KiteTicker has built-in reconnection. All callbacks (on_close, on_error, on_reconnect) are logged |
| **Graceful shutdown** | SIGINT/SIGTERM handlers square off positions before exit |
| **Order state tracking** | Each order polled through PLACED -> OPEN -> COMPLETE/REJECTED/CANCELLED |
| **Duplicate prevention** | Pre-trade check ensures no existing position for the same instrument |
| **Dead man's switch** | Even if the bot crashes, Zerodha auto-squares-off MIS positions at 3:20 PM |
| **Position reconciliation** | Every 30s, positions are synced with Kite API to catch external changes |

---

## Deployment

### VPS Setup

For production, deploy on a VPS close to NSE servers for lowest latency:

| Provider | Region | Recommendation |
|---|---|---|
| AWS | `ap-south-1` (Mumbai) | Best latency to NSE |
| DigitalOcean | `BLR1` (Bangalore) | Good budget option |
| Linode | Mumbai | Alternative |

**Minimum specs:** 1 vCPU, 1 GB RAM, Ubuntu 22.04 LTS

```bash
# On your VPS:
sudo apt update && sudo apt install python3.10 python3.10-venv python3-pip git -y
git clone https://github.com/WOLFIEEEE/PythonBot.git
cd PythonBot
python3 -m venv venv
source venv/bin/activate
pip install -r requirements.txt
cp config/.env.example config/.env
nano config/.env   # Add your credentials
mkdir -p logs
```

### systemd Service

Create `/etc/systemd/system/kite-bot.service`:

```ini
[Unit]
Description=Kite Intraday Trading Bot
After=network.target

[Service]
Type=simple
User=traderbot
WorkingDirectory=/home/traderbot/PythonBot
ExecStart=/home/traderbot/PythonBot/venv/bin/python main.py
Restart=on-failure
RestartSec=10
Environment=PYTHONUNBUFFERED=1

[Install]
WantedBy=multi-user.target
```

```bash
sudo systemctl daemon-reload
sudo systemctl enable kite-bot
sudo systemctl start kite-bot
sudo systemctl status kite-bot

# View logs
sudo journalctl -u kite-bot -f
```

### Cron Jobs

Optionally, start/stop the bot on a schedule:

```bash
crontab -e
```

```cron
# Start bot at 9:00 AM IST (before market open)
0 9 * * 1-5 cd /home/traderbot/PythonBot && /home/traderbot/PythonBot/venv/bin/python main.py &

# Kill bot at 4:00 PM IST (after market close + buffer)
0 16 * * 1-5 pkill -f "python main.py"

# Daily backup of trade database
0 17 * * 1-5 cp /home/traderbot/PythonBot/trades.db /home/traderbot/backups/trades_$(date +\%Y\%m\%d).db
```

---

## Security Best Practices

1. **Never commit secrets.** `config/.env` and `access_token.txt` are in `.gitignore` — keep it that way.
2. **Use environment variables** for API keys. The bot loads them via `python-dotenv`.
3. **Dedicated Zerodha account.** Don't use your personal trading account for the bot.
4. **Enable 2FA** on the Zerodha account.
5. **Rate limits.** Kite Connect allows ~3 order requests/second and ~1 historical data request/second. The bot respects these with built-in delays and retry backoff.
6. **File permissions.** On a VPS, restrict access:
   ```bash
   chmod 600 config/.env
   chmod 600 access_token.txt
   ```
7. **Firewall.** No inbound ports are needed — the bot only makes outbound connections.
8. **Keep dependencies updated:**
   ```bash
   pip install --upgrade -r requirements.txt
   ```

---

## Testing Checklist

Before going live with real money, verify **every item**:

- [ ] Authentication works and tokens are cached correctly for the day
- [ ] WebSocket connects and receives ticks for all watchlist instruments
- [ ] Candle aggregation produces correct OHLCV from raw ticks
- [ ] Each strategy generates correct signals on known historical data
- [ ] Position sizing calculation matches expected values
- [ ] Orders are placed correctly (use small quantities first)
- [ ] SL and target orders are placed immediately after entry fill
- [ ] Trailing SL modifies SL orders correctly as price moves
- [ ] Auto square-off happens at `SQUARE_OFF_TIME` (15:10)
- [ ] Daily loss circuit breaker triggers correctly
- [ ] Max trades limit prevents excess trading
- [ ] Max positions limit prevents over-exposure
- [ ] Telegram notifications are sent for all events
- [ ] Trade logs are written to the database correctly
- [ ] Backtest results are reasonable (no look-ahead bias)
- [ ] Graceful shutdown (`Ctrl+C`) squares off all positions
- [ ] Bot recovers from WebSocket disconnection
- [ ] Bot handles order rejections (insufficient margin, frozen stock)
- [ ] Charges estimation matches Zerodha's actual charges (approximately)

**Recommended:** Start with **1 share per trade** on a live account to validate the full flow before increasing capital.

---

## Troubleshooting

| Problem | Solution |
|---|---|
| `KITE_API_KEY and KITE_API_SECRET must be set` | Copy `.env.example` to `.env` and fill in your credentials |
| `TokenException: Token is invalid or has expired` | Delete `access_token.txt` and re-authenticate |
| `No instruments resolved -- exiting` | Check that your watchlist symbols match NSE tradingsymbols exactly |
| WebSocket keeps disconnecting | Check internet stability. KiteTicker auto-reconnects. Check logs for error codes |
| Orders getting rejected | Check available margin in Kite. Ensure the stock is not in trade-to-trade (T2T) segment |
| `ModuleNotFoundError` | Activate your virtual environment: `source venv/bin/activate` |
| Database errors | Delete `trades.db` and restart (tables will be recreated). For PostgreSQL, check connection string |
| No Telegram notifications | Verify `TELEGRAM_BOT_TOKEN` and `TELEGRAM_CHAT_ID` in `.env`. Message the bot first to activate it |
| Bot not placing trades | Check logs. Likely blocked by pre-trade checks (daily loss limit, max trades, time gate). Increase limits in `settings.py` if appropriate |
| `Permission denied` on VPS | Run `chmod 600 config/.env access_token.txt` and ensure the service runs as the correct user |

---

## Disclaimer

**This software is for educational and informational purposes only.**

- Automated trading involves **significant financial risk**. You can lose all of your invested capital.
- **No guaranteed profits.** Past backtest performance does not predict future results.
- **Paper trade first.** Always test extensively with small capital before deploying with real money.
- **Zerodha TOS.** Verify that your usage of the Kite Connect API complies with Zerodha's Terms of Service.
- **SEBI Regulations.** Retail algo trading via broker APIs is permitted, but ensure compliance with the latest SEBI guidelines on algorithmic trading for retail investors.
- **You are solely responsible** for any financial losses incurred by using this bot.
- The authors and contributors of this project are **not liable** for any trading losses.

---

## License

This project is provided as-is for educational purposes. Use at your own risk.
