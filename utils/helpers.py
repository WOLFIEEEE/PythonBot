"""
Miscellaneous helper functions — time checks, instrument lookups, charges.
"""

from __future__ import annotations

import time
from datetime import datetime
from functools import wraps
from typing import Any, Callable

import pytz

from config import settings
from utils.logger import get_logger

log = get_logger(__name__)

IST = pytz.timezone("Asia/Kolkata")


def now_ist() -> datetime:
    """Current datetime in IST."""
    return datetime.now(IST)


def _hhmm_to_time(hhmm: str):
    """Parse 'HH:MM' string to a time object."""
    h, m = map(int, hhmm.split(":"))
    return datetime.min.replace(hour=h, minute=m).time()


def is_market_open() -> bool:
    t = now_ist().time()
    return _hhmm_to_time(settings.MARKET_OPEN) <= t <= _hhmm_to_time(settings.MARKET_CLOSE)


def is_past_square_off_time() -> bool:
    return now_ist().time() >= _hhmm_to_time(settings.SQUARE_OFF_TIME)


def is_past_no_new_trades_time() -> bool:
    return now_ist().time() >= _hhmm_to_time(settings.NO_NEW_TRADES_AFTER)


def is_market_day() -> bool:
    """
    Check if today is a trading day.
    Filters weekends AND known NSE holidays for 2025-2026.
    """
    today = now_ist()
    if today.weekday() >= 5:
        return False
    return today.date() not in NSE_HOLIDAYS


# ── NSE Holidays (2025-2026) ─────────────────────────────────────────
# Source: NSE circulars. Update annually.
from datetime import date as _date

NSE_HOLIDAYS: set = {
    # 2025
    _date(2025, 2, 26),   # Mahashivratri
    _date(2025, 3, 14),   # Holi
    _date(2025, 3, 31),   # Id-Ul-Fitr (Ramadan)
    _date(2025, 4, 10),   # Shri Mahavir Jayanti
    _date(2025, 4, 14),   # Dr. Baba Saheb Ambedkar Jayanti
    _date(2025, 4, 18),   # Good Friday
    _date(2025, 5, 1),    # Maharashtra Day
    _date(2025, 6, 7),    # Bakri Id (Eid ul-Adha)
    _date(2025, 8, 15),   # Independence Day
    _date(2025, 8, 16),   # Janmashtami
    _date(2025, 10, 2),   # Mahatma Gandhi Jayanti
    _date(2025, 10, 21),  # Dussehra
    _date(2025, 10, 22),  # Dussehra (Vijaya Dashami)
    _date(2025, 11, 5),   # Diwali (Laxmi Puja)
    _date(2025, 11, 6),   # Diwali (Balipratipada)
    _date(2025, 11, 26),  # Guru Nanak Jayanti
    _date(2025, 12, 25),  # Christmas
    # 2026
    _date(2026, 1, 26),   # Republic Day
    _date(2026, 2, 17),   # Mahashivratri
    _date(2026, 3, 3),    # Holi
    _date(2026, 3, 20),   # Id-Ul-Fitr (Ramadan)
    _date(2026, 3, 30),   # Shri Ram Navami
    _date(2026, 4, 3),    # Good Friday
    _date(2026, 4, 14),   # Dr. Baba Saheb Ambedkar Jayanti
    _date(2026, 5, 1),    # Maharashtra Day
    _date(2026, 5, 25),   # Buddha Purnima
    _date(2026, 5, 28),   # Bakri Id (Eid ul-Adha)
    _date(2026, 8, 14),   # Janmashtami
    _date(2026, 8, 15),   # Independence Day
    _date(2026, 10, 2),   # Mahatma Gandhi Jayanti
    _date(2026, 10, 12),  # Dussehra
    _date(2026, 10, 23),  # Diwali (Laxmi Puja)
    _date(2026, 11, 16),  # Guru Nanak Jayanti
    _date(2026, 12, 25),  # Christmas
}


# ── Instrument lookup ────────────────────────────────────────────────
def build_instrument_map(kite) -> dict[str, int]:
    """
    Fetch all NSE instruments and return {tradingsymbol: instrument_token}.
    """
    instruments = kite.instruments("NSE")
    return {row["tradingsymbol"]: row["instrument_token"] for row in instruments}


def resolve_tokens(instrument_map: dict[str, int], watchlist: list[str]) -> dict[str, int]:
    """
    Given watchlist entries like 'NSE:RELIANCE', return
    {'RELIANCE': instrument_token, …}.
    """
    resolved: dict[str, int] = {}
    for entry in watchlist:
        symbol = entry.split(":")[-1]
        token = instrument_map.get(symbol)
        if token is None:
            log.warning("Instrument %s not found — skipping.", symbol)
            continue
        resolved[symbol] = token
    return resolved


# ── Charges calculator ───────────────────────────────────────────────
def calculate_charges(buy_price: float, sell_price: float, qty: int) -> float:
    """
    Estimate Zerodha intraday charges and return total charges (INR).
    """
    buy_turnover = buy_price * qty
    sell_turnover = sell_price * qty
    turnover = buy_turnover + sell_turnover

    brokerage_buy = min(settings.BROKERAGE_PER_ORDER, buy_turnover * settings.BROKERAGE_PCT / 100)
    brokerage_sell = min(settings.BROKERAGE_PER_ORDER, sell_turnover * settings.BROKERAGE_PCT / 100)
    brokerage = brokerage_buy + brokerage_sell

    stt = sell_turnover * settings.STT_PCT / 100
    txn_charges = turnover * settings.TRANSACTION_CHARGES_PCT / 100
    gst = (brokerage + txn_charges) * settings.GST_PCT / 100
    sebi = turnover * settings.SEBI_CHARGES_PER_CRORE / 1e7
    stamp = buy_turnover * settings.STAMP_DUTY_PCT / 100

    return brokerage + stt + txn_charges + gst + sebi + stamp


# ── Retry decorator ─────────────────────────────────────────────────
def retry(
    max_retries: int = settings.API_RETRY_COUNT,
    backoff: int = settings.API_RETRY_BACKOFF,
    exceptions: tuple = (Exception,),
) -> Callable:
    """Decorator: retry with exponential backoff on specified exceptions."""

    def decorator(func: Callable) -> Callable:
        @wraps(func)
        def wrapper(*args: Any, **kwargs: Any) -> Any:
            last_exc: Exception | None = None
            for attempt in range(1, max_retries + 1):
                try:
                    return func(*args, **kwargs)
                except exceptions as exc:
                    last_exc = exc
                    wait = backoff ** attempt
                    log.warning(
                        "%s attempt %d/%d failed (%s). Retrying in %ds…",
                        func.__name__,
                        attempt,
                        max_retries,
                        exc,
                        wait,
                    )
                    time.sleep(wait)
            raise last_exc  # type: ignore[misc]

        return wrapper

    return decorator
