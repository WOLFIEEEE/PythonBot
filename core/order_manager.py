"""
Order placement, modification, cancellation, and lifecycle management.
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
        """
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
        Place a stop-loss order. direction should be the EXIT side
        (SELL if long, BUY if short).
        """
        params: dict[str, Any] = {
            "variety": "regular",
            "exchange": settings.EXCHANGE,
            "tradingsymbol": symbol,
            "transaction_type": direction,
            "quantity": quantity,
            "product": settings.PRODUCT_TYPE,
            "order_type": "SL-M",
            "trigger_price": trigger_price,
        }
        if price is not None:
            params["order_type"] = "SL"
            params["price"] = price

        try:
            order_id = self.kite.place_order(**params)
            log.info(
                "SL order placed: %s %s trigger=%.2f order_id=%s",
                direction, symbol, trigger_price, order_id,
            )
            return order_id
        except (kite_exc.OrderException, kite_exc.InputException) as exc:
            log.error("SL order failed for %s: %s", symbol, exc)
            notify_order_error(symbol, str(exc))
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
    ) -> bool:
        """Modify an existing SL order's trigger price."""
        params: dict[str, Any] = {
            "variety": "regular",
            "order_id": order_id,
            "trigger_price": new_trigger,
        }
        if new_price is not None:
            params["price"] = new_price
            params["order_type"] = "SL"
        else:
            params["order_type"] = "SL-M"

        try:
            self.kite.modify_order(**params)
            log.info("SL order %s modified — new trigger=%.2f", order_id, new_trigger)
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
