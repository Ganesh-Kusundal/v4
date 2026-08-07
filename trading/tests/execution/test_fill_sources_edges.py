"""Fill-source edge-branch contract tests — ported from v3.

Adapted for v4 API: FillSource.submit() → tuple[Order, Fill | None],
PaperFillSource takes cache (not gateway), BrokerFillSource takes broker,
ReplayFillSource takes fills list.  v3 gateway/adapter patterns are
replaced with simpler v4 constructions.
"""

from __future__ import annotations

from datetime import UTC, datetime
from decimal import Decimal

from tradex_domain import (
    Equity,
    Fill,
    OrderId,
    OrderRequest,
    OrderSide,
    OrderStatus,
    OrderType,
    Price,
    Quantity,
)

from tradex_trading.execution.fill_sources import (
    BrokerFillSource,
    PaperFillSource,
    ReplayFillSource,
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


def _make_fill(order_id: str = "f-1", price: int = 100) -> Fill:
    return Fill(
        order_id=OrderId(value=order_id),
        instrument=_eq(),
        side=OrderSide.BUY,
        quantity=Quantity(value=Decimal("10")),
        price=Price(value=Decimal(price)),
        timestamp=datetime(2026, 8, 1, tzinfo=UTC),
    )


# ---------------------------------------------------------------------------
# SimulatedFillSource
# ---------------------------------------------------------------------------


def test_simulated_source_returns_filled_order_with_fill() -> None:
    source = SimulatedFillSource()
    order, fill = source.submit(_request())
    assert order.status is OrderStatus.FILLED
    assert fill is not None
    assert fill.price.value == Decimal("100")


# ---------------------------------------------------------------------------
# PaperFillSource
# ---------------------------------------------------------------------------


def test_paper_source_returns_filled_order() -> None:
    source = PaperFillSource()
    order, fill = source.submit(_request())
    assert order.status is OrderStatus.FILLED
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


def test_broker_source_without_submit_still_returns_ack() -> None:
    # v4 BrokerFillSource always creates an ACK order even without submit_order
    source = BrokerFillSource(broker=_NoMethodBroker())
    order, fill = source.submit(_request())
    assert order.status is OrderStatus.ACK


# ---------------------------------------------------------------------------
# ReplayFillSource
# ---------------------------------------------------------------------------


def test_replay_source_replays_historical_fills() -> None:
    historical_fill = _make_fill("replay-1", price=95)
    source = ReplayFillSource(fills=[historical_fill])
    order, fill = source.submit(_request())
    assert order.status is OrderStatus.FILLED
    assert fill is not None
    assert fill.price.value == Decimal("95")


def test_replay_source_exhausts_fills() -> None:
    source = ReplayFillSource(fills=[_make_fill()])
    # First submit — uses the historical fill
    order1, fill1 = source.submit(_request())
    assert fill1 is not None
    # Second submit — no more fills, returns ACK without fill
    order2, fill2 = source.submit(_request())
    assert order2.status is OrderStatus.ACK
    assert fill2 is None


def test_replay_source_empty_fills_returns_ack() -> None:
    source = ReplayFillSource(fills=[])
    order, fill = source.submit(_request())
    assert order.status is OrderStatus.ACK
    assert fill is None
