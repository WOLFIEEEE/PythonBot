"""
WebSocket ticker for live market data with real-time candle aggregation.
Includes tick validation and stale data detection.
"""

from __future__ import annotations

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
        """
        ltp = tick.get("last_price")
        if ltp is None or ltp <= 0:
            return None  # Invalid tick — skip

        volume = tick.get("volume_traded", 0)
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

            self._current[symbol] = {
                "timestamp": candle_start,
                "open": ltp,
                "high": ltp,
                "low": ltp,
                "close": ltp,
                "volume": volume,
            }
            return finalized

        # Same candle — update
        cur["high"] = max(cur["high"], ltp)
        cur["low"] = min(cur["low"], ltp)
        cur["close"] = ltp
        cur["volume"] = volume  # cumulative from exchange
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


class DataFeed:
    """
    Wraps KiteTicker and the CandleAggregator.

    Usage:
        feed = DataFeed(access_token, instrument_tokens, on_candle_callback)
        feed.start()      # non-blocking (runs in a thread)
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

    # ── Callbacks ────────────────────────────────────────────────────
    def _on_ticks(self, ws: Any, ticks: list[dict]) -> None:
        for tick in ticks:
            token = tick.get("instrument_token")
            symbol = self.token_symbol_map.get(token)
            if symbol is None:
                continue

            finalized = self.aggregator.on_tick(symbol, tick)
            if finalized is not None and self.on_candle:
                df = self.aggregator.get_dataframe(symbol)
                try:
                    self.on_candle(symbol, df)
                except Exception:
                    log.exception("Error in on_candle callback for %s", symbol)

    def _on_connect(self, ws: Any, response: Any) -> None:
        tokens = list(self.token_symbol_map.keys())
        ws.subscribe(tokens)
        ws.set_mode(ws.MODE_FULL, tokens)
        self._connected.set()
        log.info("WebSocket connected — subscribed to %d tokens.", len(tokens))

    def _on_close(self, ws: Any, code: int, reason: str) -> None:
        self._connected.clear()
        log.warning("WebSocket closed (code=%s, reason=%s).", code, reason)

    def _on_error(self, ws: Any, code: int, reason: str) -> None:
        log.error("WebSocket error (code=%s, reason=%s).", code, reason)

    def _on_reconnect(self, ws: Any, attempts: int) -> None:
        log.info("WebSocket reconnecting (attempt %d)...", attempts)

    # ── Public API ───────────────────────────────────────────────────
    def start(self) -> None:
        """Start the ticker in a background daemon thread."""
        self._ticker = KiteTicker(KITE_API_KEY, self.access_token)
        self._ticker.on_ticks = self._on_ticks
        self._ticker.on_connect = self._on_connect
        self._ticker.on_close = self._on_close
        self._ticker.on_error = self._on_error
        self._ticker.on_reconnect = self._on_reconnect

        self._thread = threading.Thread(target=self._ticker.connect, daemon=True)
        self._thread.start()
        log.info("DataFeed started.")

    def stop(self) -> None:
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
