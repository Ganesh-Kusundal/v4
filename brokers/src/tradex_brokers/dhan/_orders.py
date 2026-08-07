"""Dhan REST client mixin — OrdersMixin.

Mixed into :class:`~tradex_brokers.dhan.client.DhanApiClient`; the
facade owns shared state and internal helpers.
"""

from __future__ import annotations

from datetime import timedelta
from typing import TYPE_CHECKING

from tradex_domain.enums import OrderSide
from tradex_domain.errors import CapabilityNotSupportedError
from tradex_domain.execution import Order, OrderRequest, OrderResult
from tradex_domain.value_objects import OrderId

from tradex_brokers.common.client_shared import order_result_from_dict
from tradex_brokers.common.provider_common import first_mapping, unwrap_data

if TYPE_CHECKING:
    from tradex_brokers.dhan._facade import DhanClientFacade


class OrdersMixin:
    def submit_order(self: DhanClientFacade, request: OrderRequest) -> OrderId:
        """Place a new order via POST /orders."""
        correlation = (
            str(request.correlation_id.value) if request.correlation_id is not None else None
        )
        body = self._http.submit_mutation(
            "POST",
            self._url("/orders"),
            operation="Dhan order placement",
            correlation_id=correlation,
            json=self._order_payload(request))
        row = first_mapping(unwrap_data(body))
        raw = row.get("orderId", row.get("order_id"))
        if raw is None:
            raise ValueError("Dhan order response missing orderId")
        self._invalidate_after_write()
        return OrderId(value=str(raw))


    def cancel_order(self: DhanClientFacade, order_id: OrderId) -> Order:
        """Cancel an order via DELETE /orders/{id}."""
        body = self._request("DELETE", f"/orders/{order_id.value}")
        row = first_mapping(unwrap_data(body))
        self._invalidate_after_write()
        if not self._has_order_identity(row):
            return self.get_order(order_id)
        return self._order_from_row(row, fallback_id=order_id)


    def modify_order(self: DhanClientFacade, order_id: OrderId, request: OrderRequest) -> Order:
        """Modify an order via PUT /orders/{id}."""
        body = self._request(
            "PUT",
            f"/orders/{order_id.value}",

            json=self._order_payload(request))
        row = first_mapping(unwrap_data(body))
        self._invalidate_after_write()
        if not self._has_order_identity(row):
            return self.get_order(order_id)
        return self._order_from_row(row, fallback_id=order_id)


    def get_order(self: DhanClientFacade, order_id: OrderId) -> Order:
        """Lookup a single order via GET /orders/{id}."""
        body = self._request(
            "GET", f"/orders/{order_id.value}", cache_read=False
        )
        return self._order_from_row(first_mapping(unwrap_data(body)), fallback_id=order_id)


    def get_orderbook(self: DhanClientFacade) -> list[Order]:
        """Full order book via GET /orders."""
        body = self._request("GET", "/orders", cache_read=False)
        rows = unwrap_data(body)
        if not isinstance(rows, list):
            return []
        return [self._order_from_row(row) for row in rows if isinstance(row, dict)]


    def get_order_list(
        self: DhanClientFacade,
        *,
        status: str = "ALL",
        from_date: str | None = None,
        to_date: str | None = None,
        sector: str | None = None) -> list[Order]:
        """Filtered order list via GET /orders."""
        params: dict[str, object] = {"status": status}
        if from_date:
            params["fromDate"] = from_date
        if to_date:
            params["toDate"] = to_date
        if sector:
            params["sector"] = sector
        body = self._request(
            "GET", "/orders", cache_read=False, params=params
        )
        rows = unwrap_data(body)
        if not isinstance(rows, list):
            return []
        return [self._order_from_row(dict(row)) for row in rows if isinstance(row, dict)]


    def get_order_by_correlation_id(
        self: DhanClientFacade, correlation_id: str
    ) -> dict[str, object]:
        """Order lookup by external correlation ID via GET /orders/external/{id}."""
        body = self._request(
            "GET",
            f"/orders/external/{correlation_id}",

            cache_read=False)
        raw = unwrap_data(body)
        return raw if isinstance(raw, dict) else {}


    def submit_super_order(self: DhanClientFacade, request: OrderRequest) -> OrderId:
        """Place a super order via POST /super/orders."""
        if request.price is None or request.target_price is None or request.stop_loss_price is None:
            raise ValueError("Dhan super order requires price, target_price, and stop_loss_price")
        if request.side is OrderSide.BUY:
            valid = request.stop_loss_price.value < request.price.value < request.target_price.value
        else:
            valid = request.target_price.value < request.price.value < request.stop_loss_price.value
        if not valid:
            raise ValueError("Dhan super order protective prices are invalid for the order side")
        payload = self._order_payload(request)
        payload.update(
            {
                "targetPrice": float(request.target_price.value),
                "stopLossPrice": float(request.stop_loss_price.value),
                "trailingJump": float(request.trailing_jump.value)
                if request.trailing_jump is not None
                else 0.0,
            }
        )
        body = self._request("POST", "/super/orders", json=payload)
        self._invalidate_after_write()
        return self._response_order_id(body, "Dhan")


    def list_super_orders(self: DhanClientFacade) -> list[OrderResult]:
        """List all super orders via GET /super/orders."""
        body = self._request("GET", "/super/orders", cache_read=False)
        rows = unwrap_data(body)
        if not isinstance(rows, list):
            return []
        return [
            order_result_from_dict(row)
            for row in rows
            if isinstance(row, dict)
        ]


    def modify_super_order(
        self: DhanClientFacade, order_id: OrderId, request: OrderRequest
    ) -> OrderResult:
        """Modify a super order via PUT /super/orders/{id}."""
        if request.price is None or request.target_price is None or request.stop_loss_price is None:
            raise ValueError("Dhan super order requires price, target_price, and stop_loss_price")
        payload = self._order_payload(request)
        payload.update(
            {
                "superOrderId": order_id.value,
                "targetPrice": float(request.target_price.value),
                "stopLossPrice": float(request.stop_loss_price.value),
                "trailingJump": float(request.trailing_jump.value)
                if request.trailing_jump is not None
                else 0.0,
            }
        )
        body = self._request(
            "PUT", f"/super/orders/{order_id.value}", json=payload
        )
        self._invalidate_after_write()
        return order_result_from_dict(unwrap_data(body), fallback_id=order_id)

    def cancel_super_order(
        self: DhanClientFacade, order_id: OrderId, leg: str = "ENTRY"
    ) -> OrderResult:
        """Cancel a super order via DELETE /super/orders/{id}/{leg}."""
        body = self._request(
            "DELETE", f"/super/orders/{order_id.value}/{leg}")
        self._invalidate_after_write()
        return order_result_from_dict(unwrap_data(body), fallback_id=order_id)


    def submit_forever_order(self: DhanClientFacade, request: OrderRequest) -> OrderId:
        """Place a forever order via POST /forever/orders."""
        trigger = request.trigger_price or request.price
        price = request.price or request.trigger_price
        if trigger is None or price is None:
            raise ValueError("Dhan forever order requires price and trigger_price")
        payload = self._order_payload(request)
        payload.update(
            {
                "orderFlag": "SINGLE",
                "triggerPrice": float(trigger.value),
                "price": float(price.value),
            }
        )
        body = self._request(
            "POST", "/forever/orders", json=payload
        )
        self._invalidate_after_write()
        return self._response_order_id(body, "Dhan")


    def list_forever_orders(self: DhanClientFacade) -> list[OrderResult]:
        """List all forever orders via GET /forever/orders."""
        body = self._request("GET", "/forever/orders", cache_read=False)
        rows = unwrap_data(body)
        if not isinstance(rows, list):
            return []
        return [
            order_result_from_dict(row)
            for row in rows
            if isinstance(row, dict)
        ]


    def modify_forever_order(
        self: DhanClientFacade, order_id: OrderId, request: OrderRequest
    ) -> OrderResult:
        """Modify a forever order via PUT /forever/orders/{id}."""
        trigger = request.trigger_price or request.price
        price = request.price or request.trigger_price
        if trigger is None or price is None:
            raise ValueError("Dhan forever order requires price and trigger_price")
        payload = self._order_payload(request)
        payload.update(
            {
                "orderFlag": "SINGLE",
                "triggerPrice": float(trigger.value),
                "price": float(price.value),
            }
        )
        payload.pop("dhanClientId", None)
        body = self._request(
            "PUT", f"/forever/orders/{order_id.value}", json=payload)
        self._invalidate_after_write()
        return order_result_from_dict(unwrap_data(body), fallback_id=order_id)


    def cancel_forever_order(self: DhanClientFacade, order_id: OrderId) -> OrderResult:
        """Cancel a forever order via DELETE /forever/orders/{id}."""
        body = self._request(
            "DELETE", f"/forever/orders/{order_id.value}")
        self._invalidate_after_write()
        return order_result_from_dict(unwrap_data(body), fallback_id=order_id)


    def submit_slice_order(
        self: DhanClientFacade,
        request: OrderRequest,
        slices: int,
        interval: timedelta | None) -> list[OrderId]:
        """Place a sliced order via POST /orders/slicing."""
        if slices <= 0:
            raise ValueError("slices must be positive")
        if interval is not None:
            raise CapabilityNotSupportedError(
                "Dhan server-side slicing does not support interval scheduling"
            )
        payload = self._order_payload(request)
        payload["sliceCount"] = slices
        body = self._request("POST", "/orders/slicing", json=payload)
        row = first_mapping(unwrap_data(body))
        raw_ids = row.get("orderIds", row.get("order_ids"))
        if isinstance(raw_ids, list):
            self._invalidate_after_write()
            return [OrderId(value=str(value)) for value in raw_ids]
        self._invalidate_after_write()
        return [self._response_order_id(body, "Dhan")]

