"""Wave C1 — OMS full event-set coverage tests.

Verifies that every lifecycle point in the execution engine appends the
correct domain event to the durable event store.  No mocks: uses the
InMemoryEventStore (real implementation) and real engine pipeline.

Lifecycle points under test:
  1. Order submitted          → OrderPlaced
  2. Risk approved            → RiskDecision(approved=True)
  3. Risk rejected            → RiskDecision(approved=False) + OrderRejected
  4. Broker request           → BrokerOrderRequested  (before fill-source call)
  5. Broker ack               → BrokerOrderAcknowledged  (after fill-source returns)
  6. Fill (full / partial)    → OrderFilled
  7. Cancel request           → CancelRequested  (before venue dispatch)
  8. Cancel ack               → OrderCancelled
  9. Fill-source rejection    → OrderRejected  (non-boundary exception)
 10. Unknown submission       → UnknownSubmission  (boundary-crossed flag)
 11. Reconciliation           → ReconciliationResult
 12. Live-fill bridge         → OrderFilled  (via _apply_fill subscription)
"""

from __future__ import annotations

from decimal import Decimal
from typing import Any

import pytest
from tradex_domain.enums import OrderSide, OrderStatus, OrderType, TimeInForce
from tradex_domain.events import (
    BrokerOrderAcknowledged,
    BrokerOrderRequested,
    CancelRequested,
    OrderFilled,
    ReconciliationResult,
    RiskDecision,
    UnknownSubmission,
)
from tradex_domain.execution import Fill, Order, OrderRequest
from tradex_domain.instruments import Equity
from tradex_domain.value_objects import CorrelationId, OrderId, Price, Quantity

from tradex_trading.execution.engine import ExecutionEngine
from tradex_trading.execution.fill_sources import SimulatedFillSource
from tradex_trading.execution.recovery import InMemoryEventStore
from tradex_trading.execution.risk import RiskManager
from tradex_trading.reactive.bus import ReactiveBus

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _eq() -> Equity:
    return Equity.of("NSE", "RELIANCE")


def _request(correlation_id: CorrelationId | None = None) -> OrderRequest:
    return OrderRequest(
        instrument=_eq(),
        side=OrderSide.BUY,
        order_type=OrderType.LIMIT,
        quantity=Quantity(Decimal("10")),
        price=Price(Decimal("2500")),
        correlation_id=correlation_id,
    )


def _store() -> InMemoryEventStore:
    return InMemoryEventStore()


def _engine(
    event_store: InMemoryEventStore | None = None,
    fill_source: Any = None,
    risk_manager: Any = None,
) -> ExecutionEngine:
    return ExecutionEngine(
        bus=ReactiveBus(),
        fill_source=fill_source or SimulatedFillSource(),
        risk_manager=risk_manager,
        event_store=event_store,
    )


def _event_types(store: InMemoryEventStore) -> list[str]:
    return [type(e).__name__ for e in store.replay("*")]


# ---------------------------------------------------------------------------
# Stub fill sources for edge-case scenarios
# ---------------------------------------------------------------------------

class _FailFillSource:
    """Raises on submit (non-boundary: normal rejection)."""
    submission_boundary_crossed = False

    def submit(self, request: OrderRequest) -> tuple[Order, Fill | None]:
        raise RuntimeError("venue rejected: price too low")

    def cancel(self, order_id: OrderId) -> None:
        pass

    def modify(self, order_id: OrderId, request: OrderRequest) -> None:
        pass


class _BoundaryCrossedFillSource:
    """Raises on submit with boundary_crossed=True — outcome is unknown."""
    submission_boundary_crossed = True

    def submit(self, request: OrderRequest) -> tuple[Order, Fill | None]:
        raise OSError("network timeout after broker boundary")

    def cancel(self, order_id: OrderId) -> None:
        pass

    def modify(self, order_id: OrderId, request: OrderRequest) -> None:
        pass


# ---------------------------------------------------------------------------
# 1. OrderPlaced is appended on successful submit
# ---------------------------------------------------------------------------

def test_order_placed_appended_on_submit() -> None:
    store = _store()
    engine = _engine(event_store=store)

    engine.submit(_request())

    assert "OrderPlaced" in _event_types(store)


# ---------------------------------------------------------------------------
# 2. RiskDecision(approved=True) is appended when risk passes
# ---------------------------------------------------------------------------

def test_risk_decision_approved_appended_on_risk_pass() -> None:
    store = _store()
    # Generous notional cap — 10 × 2500 = 25_000 passes easily.
    engine = _engine(
        event_store=store,
        risk_manager=RiskManager(max_order_value=Decimal("1_000_000")),
    )

    engine.submit(_request())

    types = _event_types(store)
    assert "RiskDecision" in types
    decisions = [e for e in store.replay("*") if isinstance(e, RiskDecision)]
    assert len(decisions) == 1
    assert decisions[0].approved is True


# ---------------------------------------------------------------------------
# 3. RiskDecision(approved=False) + OrderRejected appended on risk rejection
# ---------------------------------------------------------------------------

def test_risk_decision_rejected_and_order_rejected_appended() -> None:
    store = _store()
    # Tiny notional cap — 10 × 2500 = 25_000 exceeds max_order_value=1.
    engine = _engine(
        event_store=store,
        risk_manager=RiskManager(max_order_value=Decimal("1")),
    )

    engine.submit(_request())

    types = _event_types(store)
    assert "RiskDecision" in types
    assert "OrderRejected" in types

    decisions = [e for e in store.replay("*") if isinstance(e, RiskDecision)]
    assert len(decisions) == 1
    assert decisions[0].approved is False
    assert "risk" in decisions[0].reason.lower()


# ---------------------------------------------------------------------------
# 4 + 5. BrokerOrderRequested appears before BrokerOrderAcknowledged
# ---------------------------------------------------------------------------

def test_broker_request_and_ack_appended_in_order() -> None:
    store = _store()
    engine = _engine(event_store=store)

    engine.submit(_request())

    types = _event_types(store)
    assert "BrokerOrderRequested" in types
    assert "BrokerOrderAcknowledged" in types

    req_idx = types.index("BrokerOrderRequested")
    ack_idx = types.index("BrokerOrderAcknowledged")
    assert req_idx < ack_idx, "BrokerOrderRequested must precede BrokerOrderAcknowledged"


# ---------------------------------------------------------------------------
# 6a. OrderFilled appended on successful full fill
# ---------------------------------------------------------------------------

def test_order_filled_appended_on_full_fill() -> None:
    store = _store()
    engine = _engine(event_store=store)

    engine.submit(_request())

    assert "OrderFilled" in _event_types(store)


# ---------------------------------------------------------------------------
# 6b. OrderFilled appended on partial fill (fill qty < order qty)
# ---------------------------------------------------------------------------

def test_order_filled_appended_on_partial_fill() -> None:
    """Partial fill from the fill source still appends OrderFilled."""
    import uuid

    class _PartialFillSource:
        submission_boundary_crossed = False
        position_projection_owned = False

        def submit(self, request: OrderRequest) -> tuple[Order, Fill | None]:
            order = Order(
                order_id=OrderId(str(uuid.uuid4())),
                instrument=request.instrument,
                side=request.side,
                order_type=request.order_type,
                quantity=request.quantity,
                price=request.price,
                time_in_force=request.time_in_force,
                status=OrderStatus.PARTIALLY_FILLED,
                filled_quantity=Quantity(Decimal("3")),
            )
            fill = Fill(
                order_id=order.order_id,
                instrument=request.instrument,
                side=request.side,
                quantity=Quantity(Decimal("3")),
                price=request.price or Price(Decimal("2500")),
            )
            return order, fill

        def cancel(self, order_id: OrderId) -> None:
            pass

        def modify(self, order_id: OrderId, request: OrderRequest) -> None:
            pass

    store = _store()
    engine = _engine(event_store=store, fill_source=_PartialFillSource())

    receipt = engine.submit(_request())
    assert receipt.status is not OrderStatus.REJECTED

    filled = [e for e in store.replay("*") if isinstance(e, OrderFilled)]
    assert len(filled) == 1
    assert filled[0].fill.quantity.value == Decimal("3")


# ---------------------------------------------------------------------------
# 7 + 8. CancelRequested then OrderCancelled appended on cancel
# ---------------------------------------------------------------------------

def test_cancel_request_and_order_cancelled_appended() -> None:
    # Use an ACK-only (no-fill) fill source so the order stays open for cancel

    class _AckFillSource:
        """Returns order without a fill (broker ACK only — async fill later)."""
        submission_boundary_crossed = False
        position_projection_owned = False
        _cancelled: set[str] = set()

        def submit(self, request: OrderRequest) -> tuple[Order, Fill | None]:
            import uuid

            from tradex_domain.enums import OrderStatus
            from tradex_domain.value_objects import OrderId
            order = Order(
                order_id=OrderId(str(uuid.uuid4())),
                instrument=request.instrument,
                side=request.side,
                order_type=request.order_type,
                quantity=request.quantity,
                price=request.price,
                time_in_force=request.time_in_force,
                status=OrderStatus.ACK,
            )
            return order, None  # ACK only

        def cancel(self, order_id: OrderId) -> None:
            self._cancelled.add(order_id.value)

        def modify(self, order_id: OrderId, request: OrderRequest) -> None:
            pass

    store = _store()
    fill_src = _AckFillSource()
    engine = _engine(event_store=store, fill_source=fill_src)

    receipt = engine.submit(_request())
    assert receipt.status is not OrderStatus.REJECTED, f"Submit failed: {receipt.message}"

    engine.cancel(receipt.order_id)

    types = _event_types(store)
    assert "CancelRequested" in types
    assert "OrderCancelled" in types

    req_idx = types.index("CancelRequested")
    can_idx = types.index("OrderCancelled")
    assert req_idx < can_idx, "CancelRequested must precede OrderCancelled"


# ---------------------------------------------------------------------------
# 9. OrderRejected appended when fill source raises (non-boundary)
# ---------------------------------------------------------------------------

def test_order_rejected_appended_on_fill_source_error() -> None:
    store = _store()
    engine = _engine(event_store=store, fill_source=_FailFillSource())

    engine.submit(_request())

    assert "OrderRejected" in _event_types(store)


# ---------------------------------------------------------------------------
# 10. UnknownSubmission appended on boundary-crossed exception
# ---------------------------------------------------------------------------

def test_unknown_submission_appended_on_boundary_crossed() -> None:
    from tradex_domain.errors import OrderSubmissionUnknownError

    store = _store()
    engine = _engine(event_store=store, fill_source=_BoundaryCrossedFillSource())

    with pytest.raises(OrderSubmissionUnknownError):
        engine.submit(_request())

    assert "UnknownSubmission" in _event_types(store)


# ---------------------------------------------------------------------------
# 11. ReconciliationResult appended after reconcile()
# ---------------------------------------------------------------------------

def test_reconciliation_result_appended() -> None:
    store = _store()
    engine = _engine(event_store=store)

    engine.reconcile()

    types = _event_types(store)
    assert "ReconciliationResult" in types

    results = [e for e in store.replay("*") if isinstance(e, ReconciliationResult)]
    assert len(results) == 1
    assert results[0].drift_count == 0


def test_reconciliation_result_carries_drift_count() -> None:
    """ReconciliationResult.drift_count matches the drifts list length."""
    store = _store()
    engine = _engine(event_store=store)

    # Reconcile with no broker data — trivially zero drifts
    drifts = engine.reconcile()

    results = [e for e in store.replay("*") if isinstance(e, ReconciliationResult)]
    assert len(results) == 1
    assert results[0].drift_count == len(drifts)


# ---------------------------------------------------------------------------
# 12. OrderFilled appended from live-fill bridge (_apply_fill)
# ---------------------------------------------------------------------------

def test_live_fill_bridge_appends_order_filled() -> None:
    """An externally-published OrderFilled (live broker stream) is appended."""
    import time

    store = _store()

    class _AckOnlyFill:
        submission_boundary_crossed = False
        position_projection_owned = False

        def submit(self, request: OrderRequest) -> tuple[Order, Fill | None]:
            import uuid

            from tradex_domain.enums import OrderStatus
            from tradex_domain.value_objects import OrderId
            order = Order(
                order_id=OrderId(str(uuid.uuid4())),
                instrument=request.instrument,
                side=request.side,
                order_type=request.order_type,
                quantity=request.quantity,
                price=request.price,
                time_in_force=request.time_in_force,
                status=OrderStatus.ACK,
            )
            return order, None  # no fill yet

        def cancel(self, order_id: OrderId) -> None:
            pass

        def modify(self, order_id: OrderId, request: OrderRequest) -> None:
            pass

    bus = ReactiveBus()
    engine = ExecutionEngine(
        bus=bus,
        fill_source=_AckOnlyFill(),
        event_store=store,
    )

    receipt = engine.submit(_request())
    assert receipt.status is not OrderStatus.REJECTED

    # Count store events before live fill
    pre_count = sum(1 for _ in store.replay("*"))

    # Simulate an inbound fill from broker stream
    live_fill = Fill(
        order_id=receipt.order_id,
        instrument=_eq(),
        side=OrderSide.BUY,
        quantity=Quantity(Decimal("10")),
        price=Price(Decimal("2500")),
    )
    bus.publish(OrderFilled(fill=live_fill))
    time.sleep(0.05)  # let the Rx subscription fire

    types = _event_types(store)
    filled_count = types.count("OrderFilled")
    assert filled_count >= 1, "OrderFilled from live bridge must be appended"


# ---------------------------------------------------------------------------
# 13. No event_store = no crash (backward compat)
# ---------------------------------------------------------------------------

def test_engine_without_event_store_still_works() -> None:
    """engine.event_store=None is the default; pipeline must not crash."""
    engine = _engine(event_store=None)
    receipt = engine.submit(_request())
    assert receipt.status is not OrderStatus.REJECTED


# ---------------------------------------------------------------------------
# 14. All eight event types are storable in SQLiteEventStore
# ---------------------------------------------------------------------------

def test_all_c1_event_types_round_trip_in_sqlite(tmp_path) -> None:  # type: ignore[no-untyped-def]
    """All six new Wave C1 domain events round-trip through SQLiteEventStore."""
    from tradex_trading.execution.sqlite_event_store import SQLiteEventStore

    store = SQLiteEventStore(tmp_path / "c1.db")
    request = _request()
    import uuid

    from tradex_domain.enums import OrderStatus
    from tradex_domain.value_objects import OrderId
    order = Order(
        order_id=OrderId(str(uuid.uuid4())),
        instrument=_eq(),
        side=OrderSide.BUY,
        order_type=OrderType.LIMIT,
        quantity=Quantity(Decimal("10")),
        price=Price(Decimal("2500")),
        time_in_force=TimeInForce.DAY,
        status=OrderStatus.NEW,
    )

    store.append(RiskDecision(request=request, approved=True))
    store.append(RiskDecision(request=request, approved=False, reason="risk_check_failed"))
    store.append(BrokerOrderRequested(request=request))
    store.append(BrokerOrderAcknowledged(order=order))
    store.append(CancelRequested(order_id=order.order_id.value))
    store.append(UnknownSubmission(request=request, detail="timeout"))
    store.append(ReconciliationResult(drift_count=3, detail="3 drifts"))

    events = list(store.replay("orders"))
    order_types = [type(e).__name__ for e in events]

    # RiskDecision, BrokerOrderRequested, BrokerOrderAcknowledged, CancelRequested,
    # UnknownSubmission route to "orders" stream; ReconciliationResult → "system"
    assert "RiskDecision" in order_types
    assert "BrokerOrderRequested" in order_types
    assert "BrokerOrderAcknowledged" in order_types
    assert "CancelRequested" in order_types
    assert "UnknownSubmission" in order_types

    system_events = list(store.replay("system"))
    system_types = [type(e).__name__ for e in system_events]
    assert "ReconciliationResult" in system_types

    store.close()
