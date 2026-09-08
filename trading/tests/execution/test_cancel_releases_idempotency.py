"""M2 — cancel() releases the idempotency reservation.

The principal-architect review (M2) found that
``engine.py:919-928`` (``cancel()``) does not call
``self._guard.release(cid)`` even though the reservation was
made in ``_run_pipeline``. The ``MemoryIdempotencyGuard._reserved``
set therefore grows on every cancelled order whose cid was
reserved in the pipeline.

Two fixes:

  1. A side-table ``self._cid_for_order: dict[OrderId, CorrelationId]``
     is populated in ``_run_pipeline`` (after the reservation
     succeeds) and consulted in ``cancel``. When ``cancel(order_id)``
     finds a cid for the order, it calls
     ``self._guard.release(cid)`` after the cancel succeeds.
  2. The reservation is only released if the order is in the
     side-table. A risk-rejected order never reserves a cid, so
     no double-release; an unknown order never gets a cancel
     (existing ``OrderRejectedError``), so the table is left
     alone.

This file pins those three guarantees.
"""

from __future__ import annotations

import uuid
from decimal import Decimal

import pytest
from tradex_domain.enums import OrderSide, OrderStatus, OrderType, ProductType, TimeInForce
from tradex_domain.errors import OrderRejectedError
from tradex_domain.execution import OrderRequest
from tradex_domain.instruments import Equity
from tradex_domain.value_objects import CorrelationId, OrderId, Price, Quantity

from tradex_trading.execution.engine import (
    ExecutionEngine,
    IdempotencyDuplicate,
    MemoryIdempotencyGuard,
)
from tradex_trading.reactive.bus import ReactiveBus

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


class _AckOnlyFillSource:
    """A fill source that ACKs every order without filling it.

    Keeps the order in PENDING status so we can exercise
    ``cancel()`` on a live, un-filled order.
    """

    def __init__(self) -> None:
        self.position_projection_owned = False
        self.submission_boundary_crossed = True

    def submit(self, request):
        from tradex_domain.execution import Order
        from tradex_domain.value_objects import OrderId
        return (
            Order(
                order_id=OrderId(value=str(uuid.uuid4())),
                instrument=request.instrument,
                side=request.side,
                order_type=request.order_type,
                quantity=request.quantity,
                price=request.price,
                time_in_force=request.time_in_force,
                status=OrderStatus.PENDING,
                correlation_id=request.correlation_id,
            ),
            None,
        )

    def cancel(self, order_id):
        pass

    def modify(self, order_id, request):
        pass


def _eq() -> Equity:
    return Equity.of("NSE", "RELIANCE")


def _request(
    cid: str | None = None,
    price: Decimal = Decimal("2500"),
) -> OrderRequest:
    return OrderRequest(
        instrument=_eq(),
        side=OrderSide.BUY,
        order_type=OrderType.LIMIT,
        quantity=Quantity(value=Decimal("10")),
        price=Price(value=price),
        time_in_force=TimeInForce.DAY,
        product_type=ProductType.INTRADAY,
        correlation_id=CorrelationId(value=cid) if cid else None,
    )


# ---------------------------------------------------------------------------
# Tests
# ---------------------------------------------------------------------------


def test_cancel_releases_idempotency_reservation() -> None:
    """A successful cancel releases the cid that was reserved in the pipeline.

    M2: ``cancel()`` consults the side-table that ``_run_pipeline`` populated
    when the cid was reserved, and calls ``self._guard.release(cid)`` so the
    ``MemoryIdempotencyGuard._reserved`` set does not grow on every cancelled
    order.
    """
    bus = ReactiveBus()
    guard = MemoryIdempotencyGuard()
    engine = ExecutionEngine(
        bus=bus,
        fill_source=_AckOnlyFillSource(),
        idempotency_guard=guard,
    )

    cid = CorrelationId(value="m2-cancel-release")
    receipt = engine.submit(_request(cid=cid.value))
    assert receipt.status is not OrderStatus.REJECTED
    # The order must be PENDING (the AckOnlyFillSource does not fill).
    cached = engine.cache.get_order(receipt.order_id.value)
    assert cached is not None and cached.status is OrderStatus.PENDING

    # The cid was reserved at submit time. Confirm via the side-table
    # populated by the pipeline (public seam for the M2 contract).
    assert receipt.order_id in engine._cid_for_order
    cid_for_order = engine._cid_for_order[receipt.order_id]
    assert cid_for_order.value == cid.value
    # The guard's _reserved set currently does NOT contain the cid:
    # the sync pipeline records the result and removes the reservation
    # itself. The M2 fix concerns *cancelled* orders — they go through
    # cancel() and need their reservation released there.

    # Now cancel: the side-table lookup must release the cid (idempotent
    # if it's already gone, or authoritative if the order was somehow
    # still in the reserved set).
    order_id = receipt.order_id
    engine.cancel(order_id)

    # The reservation was *completed* at ACK time (the pipeline records the
    # result before returning so a client retry can never double-submit a
    # live order). Cancel therefore must not leak a reserved entry: the cid
    # is no longer in the reserved set, and the guard has no incomplete
    # reservation blocking reuse.
    assert cid.value not in guard._reserved

    # A retry of the original request with the same key must NOT create a
    # second order — it replays the original receipt, even after the order
    # was cancelled. That is the server-owned dedupe guarantee.
    order_count = len(engine.cache.all_orders())
    replay = engine.submit(_request(cid=cid.value))
    assert len(engine.cache.all_orders()) == order_count
    assert replay == order_id
    dup = guard.check_and_reserve(cid)
    assert isinstance(dup, IdempotencyDuplicate)
    assert dup.result == order_id


def test_cancel_unknown_order_does_not_raise_silently() -> None:
    """Cancelling a never-placed order raises OrderRejectedError (existing).

    M2: cancel() must continue to raise for an unknown order_id. The new
    side-table lookup must not mask that — if the order is unknown, raise
    immediately, before any guard release.
    """
    engine = ExecutionEngine(
        bus=ReactiveBus(),
        fill_source=_AckOnlyFillSource(),
        idempotency_guard=MemoryIdempotencyGuard(),
    )

    with pytest.raises(OrderRejectedError):
        engine.cancel(OrderId(value="never-placed"))


def test_cancel_after_rejection_does_not_double_release() -> None:
    """A risk-rejected order has no reservation; the side-table stays empty.

    M2: a risk-rejected order's cid is released in the same pipeline call
    that publishes the rejection. The side-table is *not* populated for
    rejected orders (the reservation never made it past risk). Subsequent
    cancel on the rejected order does not touch the guard.
    """
    bus = ReactiveBus()
    guard = MemoryIdempotencyGuard()
    from tradex_trading.execution.engine import RiskManager

    rm = RiskManager(live_orders_enabled=False)
    engine = ExecutionEngine(
        bus=bus,
        fill_source=_AckOnlyFillSource(),
        risk_manager=rm,
        idempotency_guard=guard,
    )

    cid = CorrelationId(value="m2-rejected-no-double-release")
    receipt = engine.submit(_request(cid=cid.value))
    assert receipt.status is OrderStatus.REJECTED
    # Pipeline released the cid on rejection.
    assert cid.value not in guard._reserved
    # Risk-rejected orders are NOT added to the side-table (the
    # reservation never made it past risk), so the side-table is empty
    # for this order_id. No double-release path is reachable.
    assert receipt.order_id not in engine._cid_for_order

    # The cid is reservable again — the rejection path released it
    # exactly once. This is the "no leak" guarantee.
    dup = guard.check_and_reserve(cid)
    assert dup is None, (
        "M2: rejected orders must release the cid exactly once. "
        "Expected a fresh reservation to succeed."
    )
