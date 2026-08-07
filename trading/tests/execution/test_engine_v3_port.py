"""Tests for v3-ported engine symbols: RiskCheckResult, OrderStore, InMemoryOrderStore."""

from __future__ import annotations

from decimal import Decimal

from tradex_domain.enums import OrderSide, OrderStatus, OrderType, ProductType, TimeInForce
from tradex_domain.execution import Order
from tradex_domain.instruments import Equity
from tradex_domain.value_objects import OrderId, Price, Quantity

from tradex_trading.execution.engine import (
    InMemoryOrderStore,
    OrderStore,
    RiskCheckResult,
)

# ---------------------------------------------------------------------------
# RiskCheckResult
# ---------------------------------------------------------------------------


class TestRiskCheckResult:
    """RiskCheckResult dataclass."""

    def test_approved_default_reason_empty(self) -> None:
        r = RiskCheckResult(approved=True)
        assert r.approved is True
        assert r.reason == ""

    def test_rejected_with_reason(self) -> None:
        r = RiskCheckResult(approved=False, reason="kill switch")
        assert r.approved is False
        assert r.reason == "kill switch"

    def test_frozen(self) -> None:
        r = RiskCheckResult(approved=True)
        try:
            r.approved = False  # type: ignore[misc]
        except AttributeError:
            pass
        else:
            raise AssertionError("RiskCheckResult should be frozen")


# ---------------------------------------------------------------------------
# OrderStore Protocol
# ---------------------------------------------------------------------------


class TestOrderStoreProtocol:
    """OrderStore is a runtime-checkable Protocol."""

    def test_in_memory_store_satisfies_protocol(self) -> None:
        store = InMemoryOrderStore()
        assert isinstance(store, OrderStore)


# ---------------------------------------------------------------------------
# InMemoryOrderStore
# ---------------------------------------------------------------------------


def _make_order(order_id: str = "ord-1") -> Order:
    return Order(
        order_id=OrderId(value=order_id),
        instrument=Equity.of("NSE", "RELIANCE"),
        side=OrderSide.BUY,
        order_type=OrderType.LIMIT,
        quantity=Quantity(value=Decimal("10")),
        price=Price(value=Decimal("2500")),
        time_in_force=TimeInForce.DAY,
        status=OrderStatus.NEW,
        product_type=ProductType.INTRADAY,
    )


class TestInMemoryOrderStore:
    """InMemoryOrderStore — dict-backed order persistence."""

    def test_upsert_and_get(self) -> None:
        store = InMemoryOrderStore()
        order = _make_order("a")
        store.upsert(order)
        assert store.get(OrderId(value="a")) is order

    def test_get_returns_none_for_missing(self) -> None:
        store = InMemoryOrderStore()
        assert store.get(OrderId(value="missing")) is None

    def test_all_orders(self) -> None:
        store = InMemoryOrderStore()
        o1 = _make_order("x")
        o2 = _make_order("y")
        store.upsert(o1)
        store.upsert(o2)
        result = store.all_orders()
        assert len(result) == 2
        ids = {o.order_id.value for o in result}
        assert ids == {"x", "y"}

    def test_upsert_overwrites(self) -> None:
        store = InMemoryOrderStore()
        o1 = _make_order("z")
        o2 = _make_order("z")  # same id
        store.upsert(o1)
        store.upsert(o2)
        assert store.get(OrderId(value="z")) is o2
        assert len(store.all_orders()) == 1
