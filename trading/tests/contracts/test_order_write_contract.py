"""Cross-provider order-WRITE contract suite.

The parity suite proves structural conformance (hasattr/isinstance); this
module pins the write-path CONTRACT every adapter must honor, so a provider
cannot drift silently:

C1. submit_order returns the provider's OrderId and passes the caller's
    correlation id through untouched (the live-fill bridge matches on it).
C2. modify_order forwards (order_id, request) and returns an Order.
C3. cancel_order forwards the id and returns an Order.
C4. get_order reaches the provider with the requested id.
C5. Unsupported capabilities fail LOUD at the adapter gate — the transport
    is never touched (call-count assertion).
C6. Paper behavioral parity: a market order fills against the tape and
    projects the position; a filled order refuses cancellation.

Live legs use scripted transports (the adapters' own seam — they receive
domain objects exactly as the REST client mixins would); paper uses the real
PaperBroker.
"""

from __future__ import annotations

import inspect
from dataclasses import dataclass, field
from decimal import Decimal

import pytest
from tradex_domain.capabilities import BrokerCapabilities
from tradex_domain.enums import OrderSide, OrderStatus, OrderType, TimeInForce
from tradex_domain.errors import CapabilityNotSupportedError, OrderRejectedError
from tradex_domain.execution import Account, Order, OrderRequest, PortfolioSnapshot
from tradex_domain.instruments import Equity
from tradex_domain.value_objects import (
    AccountId,
    CorrelationId,
    OrderId,
    Price,
    Quantity,
)

from tradex_brokers.dhan.adapter import DhanBroker
from tradex_brokers.paper.adapter import PaperBroker
from tradex_brokers.upstox.adapter import UpstoxBroker

_RELIANCE = Equity.of("NSE", "RELIANCE")


def _request(**overrides: object) -> OrderRequest:
    kwargs = dict(
        instrument=_RELIANCE,
        side=OrderSide.BUY,
        order_type=OrderType.LIMIT,
        quantity=Quantity(value=Decimal("10")),
        price=Price(value=Decimal("2500.00")),
        time_in_force=TimeInForce.DAY,
        correlation_id=CorrelationId(value="strat-abc-123"),
    )
    kwargs.update(overrides)
    return OrderRequest(**kwargs)  # type: ignore[arg-type]


@dataclass
class _ScriptedTransport:
    """Minimal provider client double: records writes, replays scripted reads."""

    order_id: str = "prov-1"
    submitted: list[OrderRequest] = field(default_factory=list)
    modified: list[tuple[OrderId, OrderRequest]] = field(default_factory=list)
    cancelled: list[OrderId] = field(default_factory=list)
    orders_read: int = 0

    # -- writes ---------------------------------------------------------
    def submit_order(self, request: OrderRequest) -> OrderId:
        self.submitted.append(request)
        return OrderId(value=self.order_id)

    def modify_order(self, order_id: OrderId, request: OrderRequest) -> Order:
        self.modified.append((order_id, request))
        return Order(
            order_id=order_id,
            instrument=request.instrument,
            side=request.side,
            order_type=request.order_type,
            quantity=request.quantity,
            price=request.price,
            time_in_force=request.time_in_force,
            status=OrderStatus.ACK,
        )

    def cancel_order(self, order_id: OrderId) -> Order:
        self.cancelled.append(order_id)
        return Order(
            order_id=order_id,
            instrument=_RELIANCE,
            side=OrderSide.BUY,
            order_type=OrderType.LIMIT,
            quantity=Quantity(value=Decimal("10")),
            price=Price(value=Decimal("2500.00")),
            time_in_force=TimeInForce.DAY,
            status=OrderStatus.CANCELLED,
        )

    # -- reads ----------------------------------------------------------
    def get_order(self, order_id: OrderId) -> Order:
        self.orders_read += 1
        return self._ack(order_id)

    def get_orderbook(self) -> list[Order]:
        return [self._ack(OrderId(value="book-1"))]

    def _ack(self, order_id: OrderId) -> Order:
        return Order(
            order_id=order_id,
            instrument=_RELIANCE,
            side=OrderSide.BUY,
            order_type=OrderType.LIMIT,
            quantity=Quantity(value=Decimal("10")),
            price=Price(value=Decimal("2500.00")),
            time_in_force=TimeInForce.DAY,
            status=OrderStatus.ACK,
        )

    def get_positions(self) -> list:  # pragma: no cover — protocol filler
        return []

    def get_holdings(self) -> list:  # pragma: no cover
        return []

    def get_account(self) -> Account:  # pragma: no cover
        return Account(account_id=AccountId(value="x"))

    def get_portfolio(self) -> PortfolioSnapshot:  # pragma: no cover
        return PortfolioSnapshot()

    def kill_switch(self, enable: bool = True) -> dict:  # pragma: no cover
        self.kill_switch_calls = getattr(self, "kill_switch_calls", 0) + 1
        return {}

    def status_kill_switch(self) -> dict:  # pragma: no cover
        return {}


_LIVE = {"dhan": DhanBroker, "upstox": UpstoxBroker}


# ---------------------------------------------------------------------------
# C1–C4: write-path plumbing identical across live providers
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("provider", ["dhan", "upstox"])
def test_submit_returns_provider_id_and_preserves_correlation(provider: str) -> None:
    transport = _ScriptedTransport(order_id=f"{provider}-77")
    broker = _LIVE[provider](transport=transport)
    broker.connect()
    oid = broker.submit_order(_request())
    assert isinstance(oid, OrderId)
    assert oid.value == f"{provider}-77"
    assert len(transport.submitted) == 1
    sent = transport.submitted[0]
    assert sent.correlation_id is not None
    assert sent.correlation_id.value == "strat-abc-123"


@pytest.mark.parametrize("provider", ["dhan", "upstox"])
def test_modify_forwards_pair_and_returns_order(provider: str) -> None:
    transport = _ScriptedTransport()
    broker = _LIVE[provider](transport=transport)
    broker.connect()
    oid = OrderId(value="open-1")
    new_price = Price(value=Decimal("2455.00"))
    result = broker.modify_order(oid, _request(price=new_price))
    assert [oid for oid, _ in transport.modified] == [oid]
    assert isinstance(result, Order)
    assert result.price is not None and result.price.value == new_price.value


@pytest.mark.parametrize("provider", ["dhan", "upstox"])
def test_cancel_forwards_id_and_returns_order(provider: str) -> None:
    transport = _ScriptedTransport()
    broker = _LIVE[provider](transport=transport)
    broker.connect()
    oid = OrderId(value="open-2")
    result = broker.cancel_order(oid)
    assert transport.cancelled == [oid]
    assert result.status == OrderStatus.CANCELLED


@pytest.mark.parametrize("provider", ["dhan", "upstox"])
def test_get_order_reaches_provider(provider: str) -> None:
    transport = _ScriptedTransport()
    broker = _LIVE[provider](transport=transport)
    broker.connect()
    order = broker.get_order(OrderId(value="raw-9"))
    assert transport.orders_read == 1
    assert order.order_id.value == "raw-9"


# ---------------------------------------------------------------------------
# C5: unsupported capability fails loud BEFORE the transport
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("provider", ["dhan", "upstox"])
def test_unsupported_write_fails_loud_without_touching_transport(provider: str) -> None:
    """Adapter-gated operations (kill switch here; super/forever/slice/eDIS
    likewise) refuse at the adapter before the provider is touched. Plain
    market/limit order-type gating lives in TradeService by design."""
    transport = _ScriptedTransport()
    broker = _LIVE[provider](transport=transport)
    broker._capabilities = BrokerCapabilities()  # claims nothing (test override)
    broker.connect()
    with pytest.raises(CapabilityNotSupportedError):
        broker.status_kill_switch()
    with pytest.raises(CapabilityNotSupportedError):
        broker.kill_switch(True)
    # The provider client saw zero traffic.
    assert getattr(transport, "kill_switch_calls", 0) == 0


# ---------------------------------------------------------------------------
# C6: Paper behavioral parity
# ---------------------------------------------------------------------------


def test_paper_market_order_fills_and_projects_position() -> None:
    broker = PaperBroker(auto_fill=True)
    broker.set_quote(_RELIANCE, ltp=Price(value=Decimal("2500.00")))
    oid = broker.submit_order(_request(order_type=OrderType.MARKET, price=None))
    # Fills evaluate on tape events — push one more tick after submit.
    broker.set_quote(_RELIANCE, ltp=Price(value=Decimal("2500.00")))
    order = broker.get_order(oid)
    assert order.status == OrderStatus.FILLED
    # The adapter projects the fill into its own position store.
    positions = broker.get_positions()
    assert len(positions) == 1
    assert positions[0].quantity.value == Decimal("10")


def test_paper_filled_order_refuses_cancel() -> None:
    from tradex_domain.errors import SessionStateError

    broker = PaperBroker(auto_fill=True)
    broker.set_quote(_RELIANCE, ltp=Price(value=Decimal("2500.00")))
    oid = broker.submit_order(_request(order_type=OrderType.MARKET, price=None))
    broker.set_quote(_RELIANCE, ltp=Price(value=Decimal("2500.00")))
    assert broker.get_order(oid).status == OrderStatus.FILLED
    with pytest.raises((OrderRejectedError, ValueError, SessionStateError)):
        broker.cancel_order(oid)


# ---------------------------------------------------------------------------
# Contract regression guard: PaperBroker.history once lost the canonical
# convenience defaults and broke the REST API — pin the uniform signature.
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("broker_cls", [DhanBroker, UpstoxBroker, PaperBroker])
def test_history_convenience_signature_uniform(broker_cls: type) -> None:
    sig = inspect.signature(broker_cls.history)
    params = list(sig.parameters.values())
    # (self, instrument, timeframe, start=None, end=None, ...)
    assert len(params) >= 5, f"{broker_cls.__name__}.history lost its defaults"
    assert params[3].default is None and params[4].default is None