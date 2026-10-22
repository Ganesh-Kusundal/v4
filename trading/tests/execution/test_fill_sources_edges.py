"""Fill-source edge-branch contract tests — ported from v3.

Adapted for v4 API: FillSource.submit() → tuple[Order, Fill | None],
PaperFillSource takes cache (not gateway), BrokerFillSource takes broker.
v3 gateway/adapter patterns are replaced with simpler v4 constructions.
"""

from __future__ import annotations

from decimal import Decimal

import pytest
from tradex_domain import (
    Equity,
    OrderId,
    OrderRequest,
    OrderSide,
    OrderStatus,
    OrderType,
    Price,
    Quantity,
)
from tradex_execution.fill_sources import (
    BrokerFillSource,
    PaperFillSource,
    SimulatedFillSource,
)


def _eq() -> Equity:
    return Equity.of("NSE", "RELIANCE")


def _request() -> OrderRequest:
    return OrderRequest(
        instrument=_eq(),
        side=OrderSide.BUY,
        order_type=OrderType.LIMIT,
        quantity=Quantity(value=Decimal("10")),
        price=Price(value=Decimal("100")),
    )


# ---------------------------------------------------------------------------
# SimulatedFillSource
# ---------------------------------------------------------------------------


def test_simulated_source_returns_ack_order_with_fill() -> None:
    source = SimulatedFillSource()
    order, fill = source.submit(_request())
    # The fill source returns the order at ACK; the engine owns ACK → FILLED
    # via on_order_filled (the OMS projection is the engine's, not the venue's).
    assert order.status is OrderStatus.ACK
    assert fill is not None
    assert fill.price.value == Decimal("100")


# ---------------------------------------------------------------------------
# PaperFillSource
# ---------------------------------------------------------------------------


def test_paper_source_returns_ack_order() -> None:
    source = PaperFillSource()
    order, fill = source.submit(_request())
    assert order.status is OrderStatus.ACK
    assert fill is not None
    # Without a cache, falls back to request price
    assert fill.price.value == Decimal("100")


# ---------------------------------------------------------------------------
# BrokerFillSource
# ---------------------------------------------------------------------------


class _OrderReturningBroker:
    def submit_order(self, request: OrderRequest) -> OrderId:
        return OrderId(value="broker-1")


def test_broker_source_returns_ack_order() -> None:
    source = BrokerFillSource(broker=_OrderReturningBroker())
    order, fill = source.submit(_request())
    assert order.status is OrderStatus.ACK
    # Broker fill source returns no immediate fill
    assert fill is None


class _NoMethodBroker:
    pass


def test_broker_source_without_submit_fails_loudly() -> None:
    """submit_order is a BrokerAdapter protocol member (defaulted on
    BaseBroker) — a broker missing it is a protocol violation and must fail
    loudly rather than returning an undispatched ACK order."""
    source = BrokerFillSource(broker=_NoMethodBroker())  # type: ignore[arg-type]
    with pytest.raises(AttributeError, match="submit_order"):
        source.submit(_request())
