"""TDD contract for the G4 generated pass-through wall in :mod:`BaseBroker`.

These tests lock in the behavior the principal-architect review flagged:
every one-line delegation to ``self._transport`` must keep its exact public
contract — name, signature, return value, and the gate policy that decides
whether a method is read-only, mutation-gated, or capability-gated.

The wall is generated from a single declarative list, so all five tests are
written against the public surface (``broker.method(...)``) rather than
against the internals of the generator. They pass for both the hand-written
implementation and any future generated implementation, as long as both
honour the contract.
"""

from __future__ import annotations

from dataclasses import dataclass
from decimal import Decimal
from typing import Any

import pytest
from tradex_domain.capabilities import BrokerCapabilities
from tradex_domain.enums import OrderSide, OrderType, TimeInForce
from tradex_domain.errors import (
    BrokerUnavailableError,
    CapabilityNotSupportedError,
    OrderRejectedError,
)
from tradex_domain.execution import OrderRequest, OrderResult
from tradex_domain.instruments import Equity
from tradex_domain.value_objects import OrderId, Quantity

from tradex_brokers.common.base import BaseBroker

_INSTRUMENT = Equity.of("NSE", "RELIANCE")


@dataclass
class _FakeOrder:
    """A stand-in for :class:`tradex_domain.execution.Order` that is easy to
    construct in tests and exposes the only attribute the wall tests care
    about: ``filled_quantity``."""

    order_id: OrderId
    instrument: Any
    filled_quantity: int = 7


class _MockTransport:
    """Minimal stand-in for a composed API client.

    Every attribute the BaseBroker pass-through wall may touch is recorded so
    the tests can assert "wall propagated the call to transport with these
    arguments, and returned the transport's result untouched."
    """

    def __init__(self) -> None:
        self.calls: list[tuple[str, tuple[Any, ...]]] = []

    # --- orders (mutation-gated) ---
    def submit_order(self, request: OrderRequest) -> OrderId:
        self.calls.append(("submit_order", (request,)))
        return OrderId("OID-1")

    def cancel_order(self, order_id: OrderId) -> _FakeOrder:
        self.calls.append(("cancel_order", (order_id,)))
        return _FakeOrder(order_id, _INSTRUMENT)

    def modify_order(self, order_id: OrderId, request: OrderRequest) -> _FakeOrder:
        self.calls.append(("modify_order", (order_id, request)))
        return _FakeOrder(order_id, _INSTRUMENT)

    # --- orders (read-only) ---
    def get_order(self, order_id: OrderId) -> _FakeOrder:
        self.calls.append(("get_order", (order_id,)))
        return _FakeOrder(order_id, _INSTRUMENT)

    def get_orderbook(self) -> list[_FakeOrder]:
        self.calls.append(("get_orderbook", ()))
        return [_FakeOrder(OrderId("X"), _INSTRUMENT, filled_quantity=1)]

    def get_order_by_correlation_id(self, tag: str) -> dict[str, object]:
        self.calls.append(("get_order_by_correlation_id", (tag,)))
        return {"tag": tag, "order_id": "OID-9"}

    # --- portfolio (read-only) ---
    def get_positions(self) -> list[object]:
        self.calls.append(("get_positions", ()))
        return ["pos-a", "pos-b"]

    def get_holdings(self) -> list[object]:
        self.calls.append(("get_holdings", ()))
        return ["h-1"]

    def get_account(self) -> object:
        self.calls.append(("get_account", ()))
        return "ACCOUNT"

    def get_portfolio(self) -> object:
        self.calls.append(("get_portfolio", ()))
        return "PORTFOLIO"

    # --- market data (read-only) ---
    def get_quote(self, instrument: Any) -> object:
        self.calls.append(("get_quote", (instrument,)))
        return f"QUOTE({instrument.symbol})"

    def ltp(self, instrument: Any) -> object:
        self.calls.append(("ltp", (instrument,)))
        return f"LTP({instrument.symbol})"

    # --- capability-gated ---
    def submit_super_order(self, request: OrderRequest) -> OrderId:
        self.calls.append(("submit_super_order", (request,)))
        return OrderId("SUPER-1")

    def modify_super_order(
        self, order_id: OrderId, request: OrderRequest
    ) -> OrderResult:
        self.calls.append(("modify_super_order", (order_id, request)))
        return OrderResult(order_id=order_id, status="MODIFIED")

    def cancel_super_order(self, order_id: OrderId, leg: str = "ENTRY") -> OrderResult:
        self.calls.append(("cancel_super_order", (order_id, leg)))
        return OrderResult(order_id=order_id, status="CANCELLED")

    def list_super_orders(self) -> list[OrderResult]:
        self.calls.append(("list_super_orders", ()))
        return [OrderResult(order_id=OrderId("S-1"), status="OPEN")]

    def kill_switch(self, enable: bool = True) -> dict[str, object]:
        self.calls.append(("kill_switch", (enable,)))
        return {"kill_switch": enable}


def _order_request() -> OrderRequest:
    return OrderRequest(
        instrument=_INSTRUMENT,
        side=OrderSide.BUY,
        order_type=OrderType.MARKET,
        quantity=Quantity(value=Decimal(1)),
        time_in_force=TimeInForce.DAY,
    )


def _make_broker(
    *, capabilities: BrokerCapabilities | None = None, allow_orders: bool = True
) -> tuple[BaseBroker, _MockTransport]:
    transport = _MockTransport()
    caps = capabilities or BrokerCapabilities(
        supports_super_order=True,
        supports_kill_switch=True,
    )
    broker = BaseBroker(
        capabilities=caps,
        transport=transport,
        allow_order_operations=allow_orders,
    )
    broker.connect()
    return broker, transport


# ---------------------------------------------------------------------------
# RED 1 — a pass-through forwards the call and returns the transport's value
# ---------------------------------------------------------------------------


def test_passthrough_method_submits_to_transport() -> None:
    broker, transport = _make_broker()
    request = _order_request()

    result = broker.submit_order(request)

    assert result == OrderId("OID-1")
    assert transport.calls == [("submit_order", (request,))]


# ---------------------------------------------------------------------------
# RED 2 — a mutation-gated method refuses when the live-order gate is off
# ---------------------------------------------------------------------------


def test_mutation_method_requires_live_order_operations() -> None:
    broker, transport = _make_broker(allow_orders=False)
    request = _order_request()

    with pytest.raises(OrderRejectedError):
        broker.submit_order(request)

    # The transport must never have been touched — the gate fires first.
    assert transport.calls == []


# ---------------------------------------------------------------------------
# RED 3 — a capability-gated method refuses when the capability is False
# ---------------------------------------------------------------------------


def test_capability_gated_method_raises_when_capability_false() -> None:
    caps = BrokerCapabilities(supports_super_order=False)
    broker, transport = _make_broker(capabilities=caps)
    request = _order_request()

    with pytest.raises(CapabilityNotSupportedError):
        broker.submit_super_order(request)

    # The capability gate fires before either the mutation gate or the
    # transport call.
    assert transport.calls == []


# ---------------------------------------------------------------------------
# RED 4 — return values propagate faithfully through the wall
# ---------------------------------------------------------------------------


def test_passthrough_returns_underlying_result() -> None:
    broker, _ = _make_broker()
    target = OrderId("OID-42")

    result = broker.get_order(target)

    # The wall must not wrap, copy, or coerce — the object returned by the
    # transport is the one the caller sees.
    assert isinstance(result, _FakeOrder)
    assert result.order_id == target
    assert result.filled_quantity == 7


# ---------------------------------------------------------------------------
# RED 5 — read methods on a not-connected broker fail the lifecycle gate
# ---------------------------------------------------------------------------


def test_lifecycle_gate_fails_when_not_connected() -> None:
    transport = _MockTransport()
    caps = BrokerCapabilities()
    broker = BaseBroker(capabilities=caps, transport=transport)
    # NB: connect() deliberately not called.

    with pytest.raises(BrokerUnavailableError):
        broker.get_order(OrderId("X"))
