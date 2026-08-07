"""Broker clients never touch real HTTP: the injected-fetch seam
(``Client.from_fetch``) is the single test entry point for the resilience
pipeline, and broker-specific field translation is exercised end-to-end
through it with recorded fixtures (no sockets).
"""

from __future__ import annotations

from decimal import Decimal
from typing import Any

from tradex_domain.enums import OrderSide, OrderStatus, OrderType
from tradex_domain.instruments import Equity
from tradex_domain.value_objects import InstrumentId
from tradex_domain.wire import InstrumentRegistry

from tradex_brokers.dhan.client import DhanApiClient


def _registry() -> InstrumentRegistry:
    reg = InstrumentRegistry()
    iid = InstrumentId.equity("NSE", "RELIANCE")
    reg.register(iid, {"key": "2885", "asset_class": "EQUITY"})
    reg.add_alias("2885", iid)
    reg.add_alias("RELIANCE", iid)
    return reg


def _fetch(rows: list[dict[str, Any]]):
    """Return a fetch callable serving the given rows for GET /orders."""
    sent: list[tuple[str, str]] = []

    def fetch(method: str, url: str, **kwargs: Any):
        sent.append((method, url))
        if "/orders" in url and method == "GET":
            return (200, {"data": rows})
        return (200, {"data": {}})

    return fetch, sent


def test_dhan_orderbook_maps_through_provider_seam() -> None:
    rows = [
        {
            "orderId": "1101",
            "securityId": "2885",
            "transactionType": "BUY",
            "orderType": "LIMIT",
            "quantity": 10,
            "price": "105.5",
            "orderStatus": "TRADED",
            "filledQty": 10,
            "correlationId": "9c14af9b-9f2b-4b32-a2b7-3e3c2d1e0001",
        }
    ]
    fetch, sent = _fetch(rows)
    client = DhanApiClient.from_fetch(
        fetch=fetch, registry=_registry(), client_id="TEST123", access_token="tok"
    )
    orders = client.get_orderbook()

    assert len(sent) == 1
    assert sent[0][0] == "GET"
    assert sent[0][1].endswith("/orders")
    assert len(orders) == 1
    order = orders[0]
    assert order.order_id.value == "1101"
    assert order.instrument.instrument_id == Equity.of("NSE", "RELIANCE").instrument_id
    assert order.side is OrderSide.BUY
    assert order.order_type is OrderType.LIMIT
    assert order.quantity.value == 10
    assert order.price is not None and order.price.value == 105.5
    assert order.status is OrderStatus.FILLED
    assert order.filled_quantity.value == 10


def test_dhan_submit_order_uses_submit_mutation_seam() -> None:
    from tradex_domain.execution import OrderRequest
    from tradex_domain.value_objects import Price, Quantity

    order_rows: list[dict[str, Any]] = []

    def fetch(method: str, url: str, **kwargs: Any):
        if method == "POST" and "/orders" in url:
            order_rows.append(kwargs.get("json", {}))
            return (200, {"data": {"orderId": "2201"}})
        return (200, {"data": {}})

    client = DhanApiClient.from_fetch(
        fetch=fetch, registry=_registry(), client_id="TEST123", access_token="tok"
    )
    request = OrderRequest(
        instrument=Equity.of("NSE", "RELIANCE"),
        side=OrderSide.BUY,
        order_type=OrderType.LIMIT,
        quantity=Quantity(value=Decimal(10)),
        price=Price(value=Decimal("105.5")),
    )
    order_id = client.submit_order(request)

    assert order_id.value == "2201"
    assert order_rows and order_rows[0]["dhanClientId"] == "TEST123"
    assert order_rows[0]["securityId"] == "2885"
    assert order_rows[0]["transactionType"] == "BUY"
    assert order_rows[0]["quantity"] == 10
    assert order_rows[0]["price"] == 105.5
