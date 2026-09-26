"""An unknown submission must be visible in the OMS, not only in a log.

The unknown-outcome path raised and appended an ``UnknownSubmission`` event,
but the order itself was never written: the OMS held no record of a request the
venue may have accepted. Reconciliation compares the local book with the
broker's, so an order that exists at the venue and not locally reads as drift
with nothing to explain it — and an operator inspecting the book cannot see
what is outstanding.
"""

from __future__ import annotations

from decimal import Decimal
from unittest.mock import MagicMock

import pytest
from tradex_domain.enums import OrderSide, OrderStatus, OrderType, TimeInForce
from tradex_domain.errors import OrderSubmissionUnknownError
from tradex_domain.execution import OrderRequest
from tradex_domain.instruments import Equity
from tradex_domain.value_objects import Price, Quantity
from tradex_execution.engine import ExecutionEngine
from tradex_execution.recovery import InMemoryEventStore
from tradex_execution.trading_cache import TradingCache
from tradex_reactive.bus import ReactiveBus

INSTRUMENT = Equity.of("NSE", "RELIANCE")


def _request() -> OrderRequest:
    return OrderRequest(
        instrument=INSTRUMENT,
        side=OrderSide.BUY,
        order_type=OrderType.LIMIT,
        quantity=Quantity(value=Decimal("10")),
        price=Price(value=Decimal("100")),
        time_in_force=TimeInForce.DAY,
    )


def _engine() -> ExecutionEngine:
    fill_source = MagicMock()
    fill_source.submission_boundary_crossed = True
    fill_source.submit.side_effect = OSError("connection lost")
    return ExecutionEngine(
        bus=ReactiveBus(),
        fill_source=fill_source,
        cache=TradingCache(),
        event_store=InMemoryEventStore(),
        cash=Decimal("100000"),
        require_durable_events=True,
    )


def test_the_unknown_order_is_recorded_in_the_oms() -> None:
    """The book must show the request the venue may still hold.

    The halt that follows an unknown outcome cancels the order, so the end
    state is CANCELLED — but it is written UNKNOWN first, which is what the
    durable record and the audit trail must show.
    """
    engine = _engine()
    try:
        with pytest.raises(OrderSubmissionUnknownError):
            engine.submit(_request())

        orders = engine.cache.all_orders()
        assert len(orders) == 1, (
            "the OMS has no record of a request the venue may have accepted"
        )
        # Reaching CANCELLED means it was recorded UNKNOWN and then halted.
        assert orders[0].status is OrderStatus.CANCELLED
        assert orders[0].quantity.value == Decimal("10")
    finally:
        engine.shutdown()


def test_the_unknown_order_is_persisted_for_recovery() -> None:
    """A restart must rebuild the outstanding order, not lose it."""
    engine = _engine()
    store = engine._event_store  # noqa: SLF001 - asserting the durable record
    try:
        with pytest.raises(OrderSubmissionUnknownError):
            engine.submit(_request())
    finally:
        engine.shutdown()

    from tradex_domain.events import OrderPlaced

    persisted = [
        e for e in store.replay("orders")
        if isinstance(e, OrderPlaced)
        and e.order.status == OrderStatus.UNKNOWN
    ]
    assert persisted, "the unknown order was never written to the event log"


def test_an_unknown_order_never_reads_as_filled() -> None:
    """An unknown submission filled nothing we know of, whatever it ends as.

    The halt may leave the order CANCELLED, which is the intended outcome — no
    live order left behind. It must never look FILLED, and the quantity the
    venue never confirmed must stay unfilled.
    """
    engine = _engine()
    try:
        with pytest.raises(OrderSubmissionUnknownError):
            engine.submit(_request())
        order = engine.cache.all_orders()[0]
        assert order.status is not OrderStatus.FILLED
        assert order.filled_quantity.value == Decimal("0"), (
            "an unknown submission filled nothing we know of"
        )
    finally:
        engine.shutdown()


def test_a_successful_order_is_unchanged() -> None:
    """The ordinary path must not gain a spurious UNKNOWN record."""
    from tradex_execution.fill_sources import SimulatedFillSource

    bus = ReactiveBus()
    engine = ExecutionEngine(
        bus=bus, fill_source=SimulatedFillSource(), cash=Decimal("100000"),
    )
    try:
        receipt = engine.submit(_request())
        assert receipt.status == OrderStatus.FILLED
        assert engine.cache.all_orders()[0].status == OrderStatus.FILLED
    finally:
        engine.shutdown()
