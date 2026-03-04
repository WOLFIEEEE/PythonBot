"""
Order placement, modification, cancellation, and lifecycle management.
Includes emergency exit logic when SL placement fails.
"""

from __future__ import annotations

import time
from typing import Any

from kiteconnect import KiteConnect
from kiteconnect import exceptions as kite_exc

from config import settings
from utils.helpers import retry
from utils.logger import get_logger
from utils.notifier import notify_order_error

log = get_logger(__name__)


class OrderManager:
    """Handles all order operations against the Kite Connect API."""

    def __init__(self, kite: KiteConnect):
        self.kite = kite
        # Track pending order IDs to prevent duplicates
        self._pending_orders: dict[str, str] = {}  # symbol -> order_id
        # Circuit limit cache: {symbol: {"lower": float, "upper": float}}
        self._circuit_limits: dict[str, dict[str, float]] = {}

    # ── Circuit limit check ───────────────────────────────────────────
    def is_at_circuit_limit(self, symbol: str, direction: str) -> bool:
        """
        Check if instrument is at a circuit limit where orders can't fill.
        At lower circuit: cannot SELL (no buyers).
        At upper circuit: cannot BUY (no sellers).
        """
        try:
            ohlc = self.kite.ohlc([f"{settings.EXCHANGE}:{symbol}"])
            data = ohlc.get(f"{settings.EXCHANGE}:{symbol}", {})
            ltp = data.get("last_price", 0)
            lower = data.get("lower_circuit_limit", 0)
            upper = data.get("upper_circuit_limit", 0)

            if lower and upper and ltp:
                self._circuit_limits[symbol] = {"lower": lower, "upper": upper}
                # At lower circuit: selling is blocked
                if direction == "SELL" and ltp <= lower:
                    log.warning(
                        "Circuit limit: %s at lower circuit (LTP=%.2f, LC=%.2f) — SELL blocked.",
                        symbol, ltp, lower,
                    )
                    return True
                # At upper circuit: buying is blocked
                if direction == "BUY" and ltp >= upper:
                    log.warning(
                        "Circuit limit: %s at upper circuit (LTP=%.2f, UC=%.2f) — BUY blocked.",
                        symbol, ltp, upper,
                    )
                    return True
        except Exception as exc:
            log.debug("Circuit limit check failed for %s: %s", symbol, exc)
        return False

    # ── Duplicate check ──────────────────────────────────────────────
    def has_pending_order(self, symbol: str, direction: str) -> bool:
        """Check if there's already a pending order for this instrument+direction."""
        key = f"{symbol}_{direction}"
        oid = self._pending_orders.get(key)
        if oid is None:
            return False
        try:
            status = self.get_order_status(oid)
            if status in ("COMPLETE", "REJECTED", "CANCELLED"):
                del self._pending_orders[key]
                return False
            return True  # still OPEN or TRIGGER PENDING
        except Exception:
            del self._pending_orders[key]
            return False

    def _track_order(self, symbol: str, direction: str, order_id: str) -> None:
        self._pending_orders[f"{symbol}_{direction}"] = order_id

    # ── Place entry ──────────────────────────────────────────────────
    @retry(
        max_retries=settings.API_RETRY_COUNT,
        exceptions=(kite_exc.NetworkException,),
    )
    def place_entry_order(
        self,
        symbol: str,
        direction: str,
        quantity: int,
        price: float | None = None,
    ) -> str | None:
        """
        Place an entry order (MARKET or LIMIT). Returns order_id or None.
        Blocks duplicates for the same symbol+direction.
        """
        if self.has_pending_order(symbol, direction):
            log.warning("Duplicate order blocked: %s %s already pending.", direction, symbol)
            return None

        # Circuit limit check: don't place orders that can't fill
        if self.is_at_circuit_limit(symbol, direction):
            notify_order_error(symbol, f"Order blocked: {symbol} at circuit limit for {direction}")
            return None

        order_type = settings.ORDER_TYPE
        params: dict[str, Any] = {
            "variety": "regular",
            "exchange": settings.EXCHANGE,
            "tradingsymbol": symbol,
            "transaction_type": direction,
            "quantity": quantity,
            "product": settings.PRODUCT_TYPE,
            "order_type": order_type,
        }
        if order_type == "LIMIT" and price is not None:
            params["price"] = price

        try:
            order_id = self.kite.place_order(**params)
            log.info(
                "Entry order placed: %s %s qty=%d order_id=%s",
                direction, symbol, quantity, order_id,
            )
            self._track_order(symbol, direction, order_id)
            return order_id
        except (kite_exc.OrderException, kite_exc.InputException) as exc:
            log.error("Entry order failed for %s: %s", symbol, exc)
            notify_order_error(symbol, str(exc))
            return None

    # ── Place stop-loss ──────────────────────────────────────────────
    @retry(
        max_retries=settings.API_RETRY_COUNT,
        exceptions=(kite_exc.NetworkException,),
    )
    def place_sl_order(
        self,
        symbol: str,
        direction: str,
        quantity: int,
        trigger_price: float,
        price: float | None = None,
    ) -> str | None:
        """
        Place a stop-loss LIMIT order (SL, not SL-M) to prevent slippage.

        SL-M (market) orders on NSE can fill at catastrophic prices during
        gap-downs or flash crashes. Using SL (limit) with a buffer caps the
        maximum slippage to SL_BUFFER_PCT beyond the trigger.

        direction = EXIT side (SELL if long, BUY if short).
        """
        # Calculate limit price with buffer to prevent catastrophic fills
        buffer = settings.SL_BUFFER_PCT / 100
        if price is None:
            # Auto-compute limit price from trigger + buffer
            if direction == "SELL":
                # Selling to exit long: limit below trigger
                price = round(trigger_price * (1 - buffer), 2)
            else:
                # Buying to exit short: limit above trigger
                price = round(trigger_price * (1 + buffer), 2)

        params: dict[str, Any] = {
            "variety": "regular",
            "exchange": settings.EXCHANGE,
            "tradingsymbol": symbol,
            "transaction_type": direction,
            "quantity": quantity,
            "product": settings.PRODUCT_TYPE,
            "order_type": "SL",
            "trigger_price": trigger_price,
            "price": price,
        }

        try:
            order_id = self.kite.place_order(**params)
            log.info(
                "SL order placed: %s %s trigger=%.2f limit=%.2f order_id=%s",
                direction, symbol, trigger_price, price, order_id,
            )
            return order_id
        except (kite_exc.OrderException, kite_exc.InputException) as exc:
            log.error("SL order failed for %s: %s", symbol, exc)
            notify_order_error(symbol, f"SL PLACEMENT FAILED: {exc}")
            return None

    # ── Place target ─────────────────────────────────────────────────
    @retry(
        max_retries=settings.API_RETRY_COUNT,
        exceptions=(kite_exc.NetworkException,),
    )
    def place_target_order(
        self,
        symbol: str,
        direction: str,
        quantity: int,
        price: float,
    ) -> str | None:
        """Place a LIMIT target order."""
        params: dict[str, Any] = {
            "variety": "regular",
            "exchange": settings.EXCHANGE,
            "tradingsymbol": symbol,
            "transaction_type": direction,
            "quantity": quantity,
            "product": settings.PRODUCT_TYPE,
            "order_type": "LIMIT",
            "price": price,
        }
        try:
            order_id = self.kite.place_order(**params)
            log.info(
                "Target order placed: %s %s price=%.2f order_id=%s",
                direction, symbol, price, order_id,
            )
            return order_id
        except (kite_exc.OrderException, kite_exc.InputException) as exc:
            log.error("Target order failed for %s: %s", symbol, exc)
            notify_order_error(symbol, str(exc))
            return None

    # ── Emergency exit ───────────────────────────────────────────────
    def emergency_exit(self, symbol: str, quantity: int, direction: str) -> str | None:
        """
        Immediately market-close a position. Used when SL order placement
        fails — we must not hold an unprotected position.
        """
        log.error(
            "EMERGENCY EXIT: %s %s qty=%d — SL could not be placed.",
            direction, symbol, quantity,
        )
        notify_order_error(symbol, "EMERGENCY EXIT — SL placement failed, closing position immediately")
        return self.place_entry_order(symbol, direction, quantity)

    # ── Modify SL (trailing) ─────────────────────────────────────────
    @retry(
        max_retries=settings.API_RETRY_COUNT,
        exceptions=(kite_exc.NetworkException,),
    )
    def modify_sl_order(
        self,
        order_id: str,
        new_trigger: float,
        new_price: float | None = None,
        direction: str = "SELL",
    ) -> bool:
        """Modify an existing SL order's trigger and limit price."""
        # Always use SL (limit) — never SL-M — to prevent catastrophic fills
        buffer = settings.SL_BUFFER_PCT / 100
        if new_price is None:
            if direction == "SELL":
                new_price = round(new_trigger * (1 - buffer), 2)
            else:
                new_price = round(new_trigger * (1 + buffer), 2)

        params: dict[str, Any] = {
            "variety": "regular",
            "order_id": order_id,
            "trigger_price": new_trigger,
            "price": new_price,
            "order_type": "SL",
        }

        try:
            self.kite.modify_order(**params)
            log.info("SL order %s modified — trigger=%.2f limit=%.2f", order_id, new_trigger, new_price)
            return True
        except (kite_exc.OrderException, kite_exc.InputException) as exc:
            log.error("SL modify failed (order %s): %s", order_id, exc)
            return False

    # ── Cancel order ─────────────────────────────────────────────────
    @retry(
        max_retries=settings.API_RETRY_COUNT,
        exceptions=(kite_exc.NetworkException,),
    )
    def cancel_order(self, order_id: str) -> bool:
        try:
            self.kite.cancel_order(variety="regular", order_id=order_id)
            log.info("Order %s cancelled.", order_id)
            return True
        except (kite_exc.OrderException, kite_exc.InputException) as exc:
            log.error("Cancel failed (order %s): %s", order_id, exc)
            return False

    # ── Square-off a single instrument ───────────────────────────────
    def square_off(self, symbol: str, quantity: int, direction: str) -> str | None:
        """
        Market-close a position. direction = exit side (SELL if long,
        BUY if short).
        """
        return self.place_entry_order(symbol, direction, quantity)

    # ── Square-off ALL MIS positions ─────────────────────────────────
    def square_off_all(self) -> None:
        """Close every open MIS position with a market order."""
        try:
            positions = self.kite.positions()
        except Exception as exc:
            log.error("Failed to fetch positions for square-off: %s", exc)
            return

        net = positions.get("net", [])
        for pos in net:
            qty = pos.get("quantity", 0)
            if qty == 0 or pos.get("product") != settings.PRODUCT_TYPE:
                continue
            symbol = pos["tradingsymbol"]
            exit_dir = "SELL" if qty > 0 else "BUY"
            abs_qty = abs(qty)
            log.info("Square-off: %s %s qty=%d", exit_dir, symbol, abs_qty)
            self.place_entry_order(symbol, exit_dir, abs_qty)

    # ── Order status helpers ─────────────────────────────────────────
    @retry(
        max_retries=settings.API_RETRY_COUNT,
        exceptions=(kite_exc.NetworkException,),
    )
    def get_order_status(self, order_id: str) -> str:
        """Return the latest status string for an order."""
        history = self.kite.order_history(order_id)
        if history:
            return history[-1].get("status", "UNKNOWN")
        return "UNKNOWN"

    def wait_for_fill(self, order_id: str, timeout: int = 30) -> str:
        """Poll until order is COMPLETE, REJECTED, or CANCELLED (or timeout)."""
        start = time.time()
        while time.time() - start < timeout:
            status = self.get_order_status(order_id)
            if status in ("COMPLETE", "REJECTED", "CANCELLED"):
                return status
            time.sleep(1)
        return "TIMEOUT"

    def get_fill_price(self, order_id: str) -> float | None:
        """Return average fill price of a completed order."""
        try:
            history = self.kite.order_history(order_id)
            for entry in reversed(history):
                if entry.get("status") == "COMPLETE":
                    return entry.get("average_price")
        except Exception as exc:
            log.error("Failed to get fill price for %s: %s", order_id, exc)
        return None

    # ── Cancel all pending orders ────────────────────────────────────
    def cancel_all_pending(self) -> None:
        """Cancel every open/trigger-pending order."""
        try:
            orders = self.kite.orders()
        except Exception as exc:
            log.error("Failed to fetch orders: %s", exc)
            return
        for order in orders:
            status = order.get("status", "")
            if status in ("OPEN", "TRIGGER PENDING"):
                self.cancel_order(order["order_id"])
        self._pending_orders.clear()
