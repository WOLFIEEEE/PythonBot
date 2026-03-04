"""
Correlation-aware position management.

Prevents taking highly correlated positions simultaneously, which would
effectively double exposure to the same market move.

Approach:
  1. Pre-defined sector groups for NSE stocks (BFSI, IT, Energy, etc.)
  2. Real-time price correlation check using rolling returns
  3. Exposure limit per sector group

Rules:
  - Max 2 positions in the same sector group
  - If rolling 20-period return correlation > 0.7, block the trade
  - This prevents scenarios like being long HDFCBANK + ICICIBANK + KOTAKBANK
    (all banking stocks that move together)
"""

from __future__ import annotations

from typing import Any

import numpy as np
import pandas as pd

from utils.logger import get_logger

log = get_logger(__name__)

# ── NSE Sector Groups ─────────────────────────────────────────────────
# Stocks within the same group tend to be highly correlated.
SECTOR_GROUPS: dict[str, list[str]] = {
    "BANKING": [
        "HDFCBANK", "ICICIBANK", "KOTAKBANK", "SBIN", "AXISBANK",
        "INDUSINDBK", "BANDHANBNK", "FEDERALBNK", "IDFCFIRSTB", "PNB",
    ],
    "IT": [
        "TCS", "INFY", "WIPRO", "HCLTECH", "TECHM", "LTIM", "MPHASIS",
        "COFORGE", "PERSISTENT", "LTTS",
    ],
    "ENERGY": [
        "RELIANCE", "ONGC", "BPCL", "IOC", "GAIL", "NTPC", "POWERGRID",
        "ADANIGREEN", "TATAPOWER", "COALINDIA",
    ],
    "AUTO": [
        "MARUTI", "TATAMOTORS", "M&M", "BAJAJ-AUTO", "HEROMOTOCO",
        "EICHERMOT", "ASHOKLEY", "TVSMOTOR", "BOSCHLTD",
    ],
    "PHARMA": [
        "SUNPHARMA", "DRREDDY", "CIPLA", "DIVISLAB", "APOLLOHOSP",
        "BIOCON", "LUPIN", "AUROPHARMA", "TORNTPHARM",
    ],
    "FMCG": [
        "ITC", "HINDUNILVR", "NESTLEIND", "BRITANNIA", "DABUR",
        "MARICO", "GODREJCP", "COLPAL", "TATACONSUM",
    ],
    "METALS": [
        "TATASTEEL", "JSWSTEEL", "HINDALCO", "VEDL", "SAIL",
        "NMDC", "NATIONALUM", "JINDALSTEL",
    ],
    "INFRA": [
        "LT", "ULTRACEMCO", "GRASIM", "ADANIENT", "ADANIPORTS",
        "DLF", "GODREJPROP", "AMBUJACEM", "SHREECEM",
    ],
    "TELECOM": [
        "BHARTIARTL", "IDEA",
    ],
    "FINANCE_NBFC": [
        "BAJFINANCE", "BAJAJFINSV", "SBILIFE", "HDFCLIFE",
        "ICICIPRULI", "MUTHOOTFIN", "CHOLAFIN", "SHRIRAMFIN",
    ],
}

# Build reverse lookup: symbol → sector
_SYMBOL_TO_SECTOR: dict[str, str] = {}
for _sector, _symbols in SECTOR_GROUPS.items():
    for _sym in _symbols:
        _SYMBOL_TO_SECTOR[_sym] = _sector

# Maximum positions allowed in the same sector
MAX_SAME_SECTOR_POSITIONS = 2

# Minimum rolling return correlation to block a trade
CORRELATION_THRESHOLD = 0.70

# Rolling window for return correlation (number of candles)
CORRELATION_LOOKBACK = 20


def get_sector(symbol: str) -> str:
    """Return the sector group for a symbol, or 'OTHER' if not mapped."""
    # Strip exchange prefix if present (e.g., "NSE:RELIANCE" → "RELIANCE")
    clean = symbol.split(":")[-1] if ":" in symbol else symbol
    return _SYMBOL_TO_SECTOR.get(clean, "OTHER")


def check_sector_exposure(
    new_symbol: str,
    open_positions: list[str],
    max_per_sector: int = MAX_SAME_SECTOR_POSITIONS,
) -> tuple[bool, str]:
    """
    Check if adding a new position would exceed sector exposure limits.

    Returns (blocked: bool, reason: str).
    """
    new_sector = get_sector(new_symbol)
    if new_sector == "OTHER":
        return False, ""

    same_sector_count = sum(
        1 for sym in open_positions if get_sector(sym) == new_sector
    )

    if same_sector_count >= max_per_sector:
        reason = (
            f"Sector exposure limit: {same_sector_count} positions already in "
            f"{new_sector} sector (max {max_per_sector}). "
            f"Existing: {[s for s in open_positions if get_sector(s) == new_sector]}"
        )
        log.info(reason)
        return True, reason

    return False, ""


def check_price_correlation(
    new_symbol_df: pd.DataFrame,
    existing_dfs: dict[str, pd.DataFrame],
    threshold: float = CORRELATION_THRESHOLD,
    lookback: int = CORRELATION_LOOKBACK,
) -> tuple[bool, str]:
    """
    Check if the new instrument's price movement is too correlated
    with any existing open position.

    Uses rolling log-returns correlation over the last `lookback` candles.
    Returns (blocked: bool, reason: str).
    """
    if not existing_dfs:
        return False, ""

    if len(new_symbol_df) < lookback + 1:
        return False, ""  # Not enough data to compute correlation

    new_returns = new_symbol_df["close"].pct_change().dropna().tail(lookback)
    if len(new_returns) < lookback:
        return False, ""

    for sym, df in existing_dfs.items():
        if len(df) < lookback + 1:
            continue

        existing_returns = df["close"].pct_change().dropna().tail(lookback)
        if len(existing_returns) < lookback:
            continue

        # Align the two series by length
        min_len = min(len(new_returns), len(existing_returns))
        r1 = new_returns.values[-min_len:]
        r2 = existing_returns.values[-min_len:]

        # Compute Pearson correlation
        if np.std(r1) == 0 or np.std(r2) == 0:
            continue

        corr = np.corrcoef(r1, r2)[0, 1]

        if not np.isnan(corr) and abs(corr) > threshold:
            reason = (
                f"High correlation ({corr:.2f}) between new position and "
                f"existing position {sym} — blocking to avoid concentrated risk."
            )
            log.info(reason)
            return True, reason

    return False, ""


def correlation_check(
    new_symbol: str,
    new_df: pd.DataFrame,
    open_positions: list[str],
    candle_data: dict[str, pd.DataFrame] | None = None,
) -> tuple[bool, str]:
    """
    Combined correlation check: sector exposure + price correlation.

    Args:
        new_symbol: Symbol of the new trade candidate
        new_df: Candle DataFrame for the new symbol
        open_positions: List of currently open position symbols
        candle_data: Optional dict of {symbol: DataFrame} for open positions
                     (needed for price correlation check)

    Returns (blocked: bool, reason: str).
    """
    if not open_positions:
        return False, ""

    # 1. Sector exposure check
    blocked, reason = check_sector_exposure(new_symbol, open_positions)
    if blocked:
        return True, reason

    # 2. Price correlation check (only if candle data is available)
    if candle_data:
        existing_dfs = {
            sym: df for sym, df in candle_data.items()
            if sym in open_positions and not df.empty
        }
        blocked, reason = check_price_correlation(new_df, existing_dfs)
        if blocked:
            return True, reason

    return False, ""
