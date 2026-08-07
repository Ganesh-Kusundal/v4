"""Upstox REST client mixin - OrdersMixin. """

from __future__ import annotations

from datetime import timedelta
from typing import TYPE_CHECKING
from uuid import uuid4

from tradex_domain.errors import CapabilityNotSupportedError
from tradex_domain.execution import Order, OrderRequest, OrderResult
from tradex_domain.value_objects import CorrelationId, OrderId, Price, Quantity

from tradex_brokers.common.client_shared import order_result_from_dict
from tradex_brokers.common.provider_common import (
    first_mapping,
    provider_key,
    unwrap_data,
)

if TYPE_CHECKING:
    from tradex_brokers.upstox._facade import UptoxFacade


class OrdersMixin:
    def submit_order(self: UptoxFacade, request: OrderRequest) -> OrderId:
        """Place a new order via POST /order/place (HFT host)."""
        url = self._url("/order/place", host="hft")
        correlation = (
            str(request.correlation_id.value) if request.correlation_id is not None else None
        )
        body = self._http.submit_mutation(
            "POST",
            url,
            operation="Upstox order placement",
            correlation_id=correlation,
            json=self._order_payload(request))
        row = first_mapping(unwrap_data(body))
        raw = row.get("order_id")
        if raw is None:
            ids = row.get("order_ids")
            raw = ids[0] if isinstance(ids, list) and ids else None
        if raw is None:
            raise ValueError("Upstox order response missing order_id")
        self._invalidate_after_write()
        return OrderId(value=str(raw))

    def cancel_order(self: UptoxFacade, order_id: OrderId) -> Order:
        """Cancel an order via DELETE /order/cancel (HFT host)."""
        body = self._request(
            "DELETE", "/order/cancel", host="hft", params={"order_id": order_id.value})
        row = first_mapping(unwrap_data(body))
        self._invalidate_after_write()
        if not self._has_order_identity(row):
            return self.get_order(order_id)
        return self._order_from_row(row, fallback_id=order_id)

    def modify_order(self: UptoxFacade, order_id: OrderId, request: OrderRequest) -> Order:
        """Modify an order via PUT /order/modify (HFT host)."""
        payload = self._order_payload(request)
        payload["order_id"] = order_id.value
        body = self._request(
            "PUT", "/order/modify", host="hft", json=payload)
        row = first_mapping(unwrap_data(body))
        self._invalidate_after_write()
        if not self._has_order_identity(row):
            return self.get_order(order_id)
        return self._order_from_row(row, fallback_id=order_id)

    def get_order(self: UptoxFacade, order_id: OrderId) -> Order:
        """Lookup a single order via GET /order/details (HFT host)."""
        body = self._request(
            "GET", "/order/details", host="hft", cache_read=False,
            params={"order_id": order_id.value})
        return self._order_from_row(first_mapping(unwrap_data(body)), fallback_id=order_id)

    def get_orderbook(self: UptoxFacade) -> list[Order]:
        """Full order book via GET /order/retrieve-all."""
        body = self._request("GET", "/order/retrieve-all", cache_read=False)
        rows = unwrap_data(body)
        if not isinstance(rows, list):
            return []
        return [self._order_from_row(row) for row in rows if isinstance(row, dict)]

    def get_order_by_correlation_id(self: UptoxFacade, tag: str) -> dict[str, object]:
        """Scan order list for a matching tag (Upstox has no direct tag lookup)."""
        body = self._request(
            "GET", "/order/retrieve-all", cache_read=False
        )
        rows = unwrap_data(body)
        if not isinstance(rows, list):
            return {}
        for row in rows:
            if isinstance(row, dict) and str(row.get("tag", "")) == tag:
                return row
        return {}

    def place_cover_order(self: UptoxFacade, request: OrderRequest, stop_loss: Price) -> OrderId:
        """Cover order: entry + stop-loss bundled."""
        payload = self._order_payload(request)
        payload["stop_loss"] = float(stop_loss.value)
        body = self._request(
            "POST", "/order/cover/place", host="hft", json=payload
        )
        self._invalidate_after_write()
        return self._response_order_id(body, "Upstox")

    def exit_cover_order(self: UptoxFacade, order_id: OrderId) -> dict[str, object]:
        """Cancel/exit a cover order by order_id."""
        body = self._request(
            "DELETE", "/order/cover/cancel", host="hft", params={"order_id": order_id.value})
        self._invalidate_after_write()
        raw = unwrap_data(body)
        return raw if isinstance(raw, dict) else {}

    def submit_super_order(self: UptoxFacade, request: OrderRequest) -> OrderId:
        """Upstox does not support super orders."""
        raise CapabilityNotSupportedError("Upstox does not support super orders")

    def submit_forever_order(self: UptoxFacade, request: OrderRequest) -> OrderId:
        """Place a forever (GTT) order via POST /order/gtt/place (HFT host)."""
        payload = {
            "type": "SINGLE",
            "instrument_token": provider_key(self._registry, request.instrument.instrument_id),
            "quantity": int(request.quantity.value),
            "product": "D",
            "transaction_type": request.side.value,
            "rules": [
                {
                    "strategy": "ENTRY",
                    "trigger_type": "IMMEDIATE",
                    "trigger_price": (
                        float(_trigger.value)
                        if (_trigger := (request.trigger_price or request.price)) is not None
                        else 0.0
                    ),
                }
            ],
        }
        if request.correlation_id is not None:
            payload["tag"] = str(request.correlation_id.value)
        body = self._request(
            "POST", "/order/gtt/place", host="hft", json=payload)
        self._invalidate_after_write()
        return self._response_order_id(body, "Upstox")

    def modify_forever_order(
        self: UptoxFacade, order_id: OrderId, request: OrderRequest
    ) -> OrderResult:
        """Modify a forever (GTT) order via PUT /order/gtt/modify/{id} (HFT host)."""
        trigger = request.trigger_price or request.price
        payload: dict[str, object] = {
            "gtt_order_id": order_id.value,
            "type": "SINGLE",
            "quantity": int(request.quantity.value),
            "rules": [
                {
                    "strategy": "ENTRY",
                    "trigger_type": "IMMEDIATE",
                    "trigger_price": float(trigger.value) if trigger is not None else 0.0,
                }
            ],
        }
        body = self._request(
            "PUT", f"/order/gtt/modify/{order_id.value}", host="hft", json=payload)
        self._invalidate_after_write()
        return order_result_from_dict(unwrap_data(body), fallback_id=order_id)

    def cancel_forever_order(self: UptoxFacade, order_id: OrderId) -> OrderResult:
        """Cancel a forever (GTT) order via DELETE /order/gtt/cancel/{id} (HFT host)."""
        body = self._request(
            "DELETE", f"/order/gtt/cancel/{order_id.value}", host="hft")
        self._invalidate_after_write()
        return order_result_from_dict(unwrap_data(body), fallback_id=order_id)

    def list_forever_orders(self: UptoxFacade) -> list[OrderResult]:
        """Upstox has no list-all GTT endpoint — honest empty list."""
        return []

    def submit_slice_order(
        self: UptoxFacade,
        request: OrderRequest,
        slices: int,
        interval: timedelta | None) -> list[OrderId]:
        """Client-side sliced order placement."""
        if slices <= 0:
            raise ValueError("slices must be positive")
        if interval is not None:
            raise CapabilityNotSupportedError(
                "Upstox client-side slicing does not support interval scheduling"
            )
        quantity = request.quantity.value
        base = quantity // slices
        remainder = quantity % slices
        if base <= 0:
            raise ValueError("quantity must be at least slices")
        order_ids: list[OrderId] = []
        for index in range(slices):
            child_quantity = base + (1 if index < remainder else 0)
            child_request = request.__class__(
                instrument=request.instrument,
                side=request.side,
                order_type=request.order_type,
                quantity=Quantity(value=child_quantity),
                price=request.price,
                trigger_price=request.trigger_price,
                time_in_force=request.time_in_force,
                product_type=request.product_type,
                correlation_id=CorrelationId(value=str(uuid4())),
                tag=request.tag)
            order_ids.append(self.submit_order(child_request))
        return order_ids

    def submit_edis(self: UptoxFacade, request: OrderRequest) -> OrderId:
        """Upstox does not support eDIS."""
        raise CapabilityNotSupportedError("Upstox does not support eDIS")

