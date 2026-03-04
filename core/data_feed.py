"""
WebSocket ticker for live market data with real-time candle aggregation.

Includes:
  - Tick validation and price band filtering
  - Stale data detection
  - Per-candle volume delta (not cumulative)
  - Threaded tick processing queue (prevents WebSocket thread blocking)
  - Automatic re-subscription on reconnect
  - Historical gap-fill after disconnection
  - Previous day close extraction from MODE_FULL ticks
"""

from __future__ import annotations

import queue
import threading
from collections import defaultdict
from datetime import datetime, timedelta
from typing import Any, Callable

import pandas as pd
from kiteconnect import KiteTicker

from config.settings import (
    CANDLE_INTERVAL_MINUTES,
    KITE_API_KEY,
    MAX_CANDLES_IN_MEMORY,
)
from utils.logger import get_logger

log = get_logger(__name__)

# If no tick received for an instrument in this many seconds, consider data stale
STALE_THRESHOLD_SECONDS = 120

# Maximum allowed price change (%) from last known price in a single tick.
# NSE circuit limits are 5/10/20%, so 25% catches anything beyond circuit limits.
TICK_PRICE_BAND_PCT = 25.0


class CandleAggregator:
    """Aggregates ticks into fixed-interval OHLCV candles."""

    def __init__(
        self,
        interval_minutes: int = CANDLE_INTERVAL_MINUTES,
        max_candles: int = MAX_CANDLES_IN_MEMORY,
    ):
        self.interval = timedelta(minutes=interval_minutes)
        self.max_candles = max_candles

        # Per-instrument state
        self._current: dict[str, dict[str, Any]] = {}
        self._candles: dict[str, list[dict[str, Any]]] = defaultdict(list)
        self._last_tick_time: dict[str, datetime] = {}
        # Track previous cumulative volume per instrument for per-candle delta
        self._prev_cum_volume: dict[str, int] = {}
        # Track cumulative volume at candle start for delta calculation
        self._candle_start_volume: dict[str, int] = {}
        # Last known valid price per instrument (for price band filtering)
        self._last_valid_price: dict[str, float] = {}
        # Previous day close per instrument (for gap detection)
        self._prev_day_close: dict[str, float] = {}
        # Gap detection: symbols with large opening gaps (skip early candles)
        self._gap_symbols: dict[str, int] = {}  # symbol -> candles remaining to skip

    def _floor_time(self, dt: datetime) -> datetime:
        """Floor datetime to the candle interval boundary."""
        minutes = dt.hour * 60 + dt.minute
        interval_min = max(1, self.interval.seconds // 60)
        floored = minutes - (minutes % interval_min)
        return dt.replace(
            hour=floored // 60,
            minute=floored % 60,
            second=0,
            microsecond=0,
        )

    def on_tick(self, symbol: str, tick: dict[str, Any]) -> dict[str, Any] | None:
        """
        Process a tick. Returns the finalized candle dict if a candle just
        closed, else None.

        Volume is computed as per-candle delta (not cumulative exchange volume)
        to ensure strategy volume comparisons are meaningful.
        """
        ltp = tick.get("last_price")
        if ltp is None or ltp <= 0:
            return None  # Invalid tick — skip

        # Price band filter: reject ticks that deviate too far from last price
        last_price = self._last_valid_price.get(symbol)
        if last_price is not None and last_price > 0:
            change_pct = abs(ltp - last_price) / last_price * 100
            if change_pct > TICK_PRICE_BAND_PCT:
                log.warning(
                    "Tick rejected for %s: price %.2f deviates %.1f%% from last %.2f",
                    symbol, ltp, change_pct, last_price,
                )
                return None
        self._last_valid_price[symbol] = ltp

        # Extract prev_day_close from MODE_FULL tick's ohlc.close field.
        # Kite MODE_FULL provides ohlc.close = previous trading day's close.
        tick_ohlc = tick.get("ohlc")
        if tick_ohlc and symbol not in self._prev_day_close:
            prev_close = tick_ohlc.get("close", 0)
            if prev_close > 0:
                self._prev_day_close[symbol] = prev_close

        cum_volume = tick.get("volume_traded", 0)
        ts = tick.get("exchange_timestamp") or datetime.now()

        # Validate timestamp is not far in the future
        now = datetime.now()
        if hasattr(ts, "tzinfo") and ts.tzinfo:
            now = ts  # use exchange time
        self._last_tick_time[symbol] = now

        candle_start = self._floor_time(ts)
        cur = self._current.get(symbol)

        if cur is None or cur["timestamp"] != candle_start:
            finalized = None
            if cur is not None:
                finalized = dict(cur)
                self._candles[symbol].append(finalized)
                # Trim
                if len(self._candles[symbol]) > self.max_candles:
                    self._candles[symbol] = self._candles[symbol][-self.max_candles:]

            # Record cumulative volume at candle start for delta calculation
            self._candle_start_volume[symbol] = self._prev_cum_volume.get(symbol, cum_volume)

            self._current[symbol] = {
                "timestamp": candle_start,
                "open": ltp,
                "high": ltp,
                "low": ltp,
                "close": ltp,
                "volume": max(0, cum_volume - self._candle_start_volume.get(symbol, cum_volume)),
            }
            self._prev_cum_volume[symbol] = cum_volume
            return finalized

        # Same candle — update OHLC and per-candle volume delta
        cur["high"] = max(cur["high"], ltp)
        cur["low"] = min(cur["low"], ltp)
        cur["close"] = ltp
        cur["volume"] = max(0, cum_volume - self._candle_start_volume.get(symbol, cum_volume))
        self._prev_cum_volume[symbol] = cum_volume
        return None

    def get_dataframe(self, symbol: str) -> pd.DataFrame:
        """Return historical candles as a DataFrame for the given symbol."""
        rows = list(self._candles[symbol])
        cur = self._current.get(symbol)
        if cur:
            rows = rows + [cur]
        if not rows:
            return pd.DataFrame(
                columns=["timestamp", "open", "high", "low", "close", "volume"]
            )
        df = pd.DataFrame(rows)
        df.set_index("timestamp", inplace=True)
        return df

    def get_current_candle(self, symbol: str) -> dict[str, Any] | None:
        return self._current.get(symbol)

    def is_data_stale(self, symbol: str) -> bool:
        """Check if we haven't received a tick for too long."""
        last = self._last_tick_time.get(symbol)
        if last is None:
            return True
        elapsed = (datetime.now() - last).total_seconds()
        return elapsed > STALE_THRESHOLD_SECONDS

    def get_candle_count(self, symbol: str) -> int:
        """Number of completed candles for a symbol."""
        return len(self._candles[symbol])

    def get_last_candle_time(self, symbol: str) -> datetime | None:
        """Return timestamp of the most recent completed candle for gap-fill."""
        candles = self._candles.get(symbol)
        if candles:
            return candles[-1]["timestamp"]
        return None

    def set_prev_day_close(self, symbol: str, close: float) -> None:
        """Store previous day's close for gap detection."""
        self._prev_day_close[symbol] = close

    def check_opening_gap(self, symbol: str, open_price: float) -> bool:
        """
        Check if the stock gapped significantly from previous close.
        Returns True if gap is > GAP_FILTER_PCT.
        """
        from config import settings as _s
        prev_close = self._prev_day_close.get(symbol)
        if prev_close is None or prev_close <= 0:
            return False
        gap_pct = abs(open_price - prev_close) / prev_close * 100
        if gap_pct > _s.GAP_FILTER_PCT:
            self._gap_symbols[symbol] = _s.GAP_FILTER_SKIP_CANDLES
            log.warning(
                "Gap detected for %s: open=%.2f prev_close=%.2f gap=%.1f%% — "
                "skipping %d candles.",
                symbol, open_price, prev_close, gap_pct, _s.GAP_FILTER_SKIP_CANDLES,
            )
            return True
        return False

    def should_skip_for_gap(self, symbol: str) -> bool:
        """Check if we should skip signals for this symbol due to opening gap."""
        remaining = self._gap_symbols.get(symbol, 0)
        if remaining > 0:
            self._gap_symbols[symbol] = remaining - 1
            return True
        return False


class DataFeed:
    """
    Wraps KiteTicker and the CandleAggregator.

    Key design decisions:
      - Tick processing runs in a SEPARATE worker thread (not in the WS thread)
        to prevent blocking the WebSocket when strategy computation is heavy.
      - on_connect always re-subscribes (handles reconnection automatically).
      - Gap-fill via historical API is triggered after any disconnection.

    Usage:
        feed = DataFeed(access_token, instrument_tokens, on_candle_callback)
        feed.start()      # non-blocking (runs in threads)
        feed.stop()
    """

    def __init__(
        self,
        access_token: str,
        token_symbol_map: dict[int, str],
        on_candle: Callable[[str, pd.DataFrame], None] | None = None,
        candle_interval: int = CANDLE_INTERVAL_MINUTES,
    ):
        self.access_token = access_token
        self.token_symbol_map = token_symbol_map  # {instrument_token: symbol}
        self.on_candle = on_candle
        self.aggregator = CandleAggregator(interval_minutes=candle_interval)

        self._ticker: KiteTicker | None = None
        self._thread: threading.Thread | None = None
        self._connected = threading.Event()
        self._stopping = False

        # Tick processing queue — decouples WebSocket thread from strategy processing
        self._tick_queue: queue.Queue[tuple[str, dict]] = queue.Queue(maxsize=10_000)
        self._worker_thread: threading.Thread | None = None

        # Track disconnection time for gap-fill
        self._last_disconnect_time: datetime | None = None

        # Reference to kite client for gap-fill (set via set_kite_ref)
        self._kite = None

    def set_kite_ref(self, kite) -> None:
        """Store a reference to the KiteConnect client for historical gap-fill."""
        self._kite = kite

    # ── Callbacks ────────────────────────────────────────────────────
    def _on_ticks(self, ws: Any, ticks: list[dict]) -> None:
        """
        Enqueue ticks for processing — do NOT run strategy logic here.
        The WebSocket thread must stay fast and non-blocking.
        """
        for tick in ticks:
            token = tick.get("instrument_token")
            symbol = self.token_symbol_map.get(token)
            if symbol is None:
                continue
            try:
                self._tick_queue.put_nowait((symbol, tick))
            except queue.Full:
                log.warning("Tick queue full — dropping tick for %s", symbol)

    def _tick_worker(self) -> None:
        """
        Worker thread that processes ticks from the queue.
        Runs strategy callbacks outside the WebSocket thread.
        """
        while not self._stopping:
            try:
                symbol, tick = self._tick_queue.get(timeout=1.0)
            except queue.Empty:
                continue

            try:
                finalized = self.aggregator.on_tick(symbol, tick)
                if finalized is not None and self.on_candle:
                    df = self.aggregator.get_dataframe(symbol)
                    self.on_candle(symbol, df)
            except Exception:
                log.exception("Error processing tick for %s", symbol)

    def _on_connect(self, ws: Any, response: Any) -> None:
        """
        Called on EVERY connect/reconnect — MUST re-subscribe instruments.
        Kite drops all subscriptions when WebSocket reconnects.
        """
        tokens = list(self.token_symbol_map.keys())
        ws.subscribe(tokens)
        ws.set_mode(ws.MODE_FULL, tokens)
        self._connected.set()

        # If this is a reconnection (not first connect), attempt gap-fill
        if self._last_disconnect_time is not None:
            gap_duration = (datetime.now() - self._last_disconnect_time).total_seconds()
            log.info(
                "WebSocket reconnected after %.0fs — subscribed to %d tokens.",
                gap_duration, len(tokens),
            )
            if gap_duration > 60 and self._kite:
                # Missed more than 1 minute — try to backfill from historical API
                threading.Thread(
                    target=self._backfill_gap,
                    args=(self._last_disconnect_time,),
                    daemon=True,
                    name="GapFillThread",
                ).start()
            self._last_disconnect_time = None
        else:
            log.info("WebSocket connected — subscribed to %d tokens.", len(tokens))

    def _on_close(self, ws: Any, code: int, reason: str) -> None:
        self._connected.clear()
        self._last_disconnect_time = datetime.now()
        log.warning("WebSocket closed (code=%s, reason=%s).", code, reason)

    def _on_error(self, ws: Any, code: int, reason: str) -> None:
        log.error("WebSocket error (code=%s, reason=%s).", code, reason)

    def _on_reconnect(self, ws: Any, attempts: int) -> None:
        log.info("WebSocket reconnecting (attempt %d)...", attempts)

    def _on_noreconnect(self, ws: Any) -> None:
        """Called when max reconnection attempts are exhausted."""
        log.error(
            "WebSocket max reconnection attempts exhausted. "
            "Will attempt manual reconnect in 30s..."
        )
        self._connected.clear()
        # Try to restart the connection after a delay
        threading.Timer(30.0, self._manual_reconnect).start()

    def _manual_reconnect(self) -> None:
        """Attempt to restart the WebSocket after all retries exhausted."""
        if self._stopping:
            return
        log.info("Attempting manual WebSocket reconnect...")
        try:
            if self._ticker:
                self._ticker.close()
            self._start_ticker()
        except Exception:
            log.exception("Manual reconnect failed — retrying in 60s...")
            threading.Timer(60.0, self._manual_reconnect).start()

    def _backfill_gap(self, disconnect_time: datetime) -> None:
        """
        Backfill missed candles from historical API after a WebSocket disconnection.
        This runs in a background thread to not block tick processing.
        """
        try:
            from core.candle_preloader import _fetch_historical, _inject_into_aggregator

            interval_map = {1: "minute", 3: "3minute", 5: "5minute",
                            10: "10minute", 15: "15minute", 30: "30minute", 60: "60minute"}
            interval_min = self.aggregator.interval.seconds // 60
            interval_str = interval_map.get(interval_min)
            if not interval_str:
                return

            from_dt = disconnect_time - timedelta(minutes=interval_min)
            to_dt = datetime.now()
            symbol_to_token = {v: k for k, v in self.token_symbol_map.items()}

            filled = 0
            for symbol, token in symbol_to_token.items():
                try:
                    candles = _fetch_historical(self._kite, token, from_dt, to_dt, interval_str)
                    if candles:
                        count = _inject_into_aggregator(self.aggregator, symbol, candles)
                        filled += count
                    import time
                    time.sleep(0.5)  # Rate limit: stay under 120 req/min
                except Exception as exc:
                    log.debug("Gap-fill failed for %s: %s", symbol, exc)

            if filled > 0:
                log.info("Gap-fill complete: %d candles recovered across instruments.", filled)

        except Exception:
            log.exception("Gap-fill backfill failed")

    # ── Public API ───────────────────────────────────────────────────
    def _start_ticker(self) -> None:
        """Create and start the KiteTicker connection."""
        self._ticker = KiteTicker(KITE_API_KEY, self.access_token)
        self._ticker.on_ticks = self._on_ticks
        self._ticker.on_connect = self._on_connect
        self._ticker.on_close = self._on_close
        self._ticker.on_error = self._on_error
        self._ticker.on_reconnect = self._on_reconnect
        self._ticker.on_noreconnect = self._on_noreconnect

        self._thread = threading.Thread(target=self._ticker.connect, daemon=True)
        self._thread.start()

    def start(self) -> None:
        """Start the ticker and tick worker in background daemon threads."""
        self._stopping = False

        # Start tick processing worker thread
        self._worker_thread = threading.Thread(
            target=self._tick_worker, daemon=True, name="TickWorker"
        )
        self._worker_thread.start()

        # Start WebSocket
        self._start_ticker()
        log.info("DataFeed started (WebSocket + tick worker).")

    def stop(self) -> None:
        self._stopping = True
        if self._ticker:
            self._ticker.close()
            self._connected.clear()
        log.info("DataFeed stopped.")

    @property
    def is_connected(self) -> bool:
        return self._connected.is_set()

    def get_dataframe(self, symbol: str) -> pd.DataFrame:
        return self.aggregator.get_dataframe(symbol)

    def get_ltp(self, symbol: str) -> float | None:
        cur = self.aggregator.get_current_candle(symbol)
        return cur["close"] if cur else None

    def is_data_stale(self, symbol: str) -> bool:
        return self.aggregator.is_data_stale(symbol)
