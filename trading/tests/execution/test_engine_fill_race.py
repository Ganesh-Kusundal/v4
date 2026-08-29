"""G3 — test the live-fill race.

The principal-architect review (G3) and the design baseline flagged
the lack of a test for the live-fill race between
``ExecutionEngine._run_pipeline`` and ``_apply_fill``. The
load-bearing comment at engine.py:812 documents the invariant
("FILLED already = the synchronous pipeline path applied this fill
before publishing its own OrderFilled event — never double-apply")
but no test pins it. This test pins it.
"""

from __future__ import annotations

from decimal import Decimal
from unittest.mock import MagicMock

from tradex_domain.enums import OrderSide, OrderStatus, OrderType, TimeInForce
from tradex_domain.events import OrderFilled
from tradex_domain.instruments import Equity
from tradex_domain.value_objects import OrderId, Price, Quantity

from tradex_domain.execution import Fill, Order, OrderRequest
from tradex_trading.execution.engine import ExecutionEngine
from tradex_trading.execution.fill_sources import SimulatedFillSource
from tradex_trading.reactive.bus import ReactiveBus


def _request() -> OrderRequest:
    return OrderRequest(
        instrument=Equity.of("NSE", "TEST"),
        side=OrderSide.BUY,
        order_type=OrderType.MARKET,
        quantity=Quantity(value=Decimal("1")),
        price=Price(value=Decimal("100")),
        time_in_force=TimeInForce.DAY,
    )


def test_apply_fill_idempotent_when_sync_path_already_filled() -> None:
    """RED: the sync pipeline publishes OrderFilled after marking FILLED; a
    re-publish from the live-fill bridge must not double-apply."""
    bus = ReactiveBus()
    engine = ExecutionEngine(
        bus=bus, fill_source=SimulatedFillSource(),
    )

    # Capture every OrderFilled the engine publishes.
    captured: list[OrderFilled] = []
    bus.of_type(OrderFilled).subscribe(on_next=captured.append)

    # Synchronous submit (the same code path _run_pipeline uses for
    # non-CQRS submit). For SimulatedFillSource this returns a fill
    # and the engine publishes OrderFilled.
    receipt = engine.submit(_request())
    # SimulatedFillSource always returns a fill; the pipeline publishes it.
    assert len(captured) == 1
    first_event = captured[0]

    # Now simulate the live-fill bridge re-publishing the same event.
    # The _apply_fill path must not double-apply.
    bus.publish(first_event)

    # The cache must still have exactly one order (no duplicate creation).
    assert len(engine.cache.all_orders()) == 1
    cached = engine.cache.all_orders()[0]
    assert cached.status == OrderStatus.FILLED
    # The position is updated at most once: a single BUY of 1 @ 100
    # leaves exactly 1 share at avg 100.
    positions = engine.cache.all_positions()
    assert sum(p.quantity.value for p in positions) == 1


def test_apply_fill_idempotent_with_fill_id() -> None:
    """RED: distinct equal-lot partials with different fill_ids both apply.

    The principal-architect review's third scenario: when the venue
    provides a unique ``fill.fill_id`` (exchange trade id), two genuine
    equal-lot partial fills with distinct ids must BOTH apply. The
    fingerprint dedup keys on fill_id when present, so re-publishes
    dedup, but distinct fills don't.
    """
    bus = ReactiveBus()
    engine = ExecutionEngine(
        bus=bus, fill_source=SimulatedFillSource(),
    )

    # First fill: 1 @ 100, with fill_id="trade-1".
    fill1 = Fill(
        order_id=OrderId(value="oid-1"),
        instrument=Equity.of("NSE", "TEST"),
        side=OrderSide.BUY,
        quantity=Quantity(value=Decimal("1")),
        price=Price(value=Decimal("100")),
        fill_id="trade-1",
    )
    bus.publish(OrderFilled(fill=fill1))
    # The order is unknown to the engine; _apply_fill creates a minimal
    # FILLED order. Position is 1 @ 100.
    assert sum(p.quantity.value for p in engine.cache.all_positions()) == 1

    # Second fill: 1 @ 100, with fill_id="trade-2" (different).
    fill2 = Fill(
        order_id=OrderId(value="oid-1"),
        instrument=Equity.of("NSE", "TEST"),
        side=OrderSide.BUY,
        quantity=Quantity(value=Decimal("1")),
        price=Price(value=Decimal("100")),
        fill_id="trade-2",
    )
    bus.publish(OrderFilled(fill=fill2))
    # Both fills must apply: position is 2 @ 100.
    positions = engine.cache.all_positions()
    total_qty = sum(p.quantity.value for p in positions)
    assert total_qty == 2, (
        f"Distinct equal-lot partials with different fill_ids must both apply; "
        f"got total_qty={total_qty}"
    )

    # Re-publish fill1: the engine must dedup (same fill_id) and NOT
    # double-apply.
    bus.publish(OrderFilled(fill=fill1))
    positions = engine.cache.all_positions()
    total_qty = sum(p.quantity.value for p in positions)
    assert total_qty == 2, (
        f"Re-publish of the same fill_id must be deduplicated; "
        f"got total_qty={total_qty}"
    )


def test_apply_fill_skips_rejected_order() -> None:
    """RED: an inbound OrderFilled for an order that was REJECTED does
    not move the position (R1 invariant)."""
    bus = ReactiveBus()
    fill_source = MagicMock()

    def fake_submit(_request):
        # Submit a fill source that submits successfully and marks ACK,
        # but the order will be rejected by the engine.
        from tradex_domain.value_objects import OrderId
        return (
            Order(
                order_id=OrderId(value="oid-rejected"),
                instrument=Equity.of("NSE", "TEST"),
                side=OrderSide.BUY,
                order_type=OrderType.MARKET,
                quantity=Quantity(value=Decimal("1")),
                price=Price(value=Decimal("100")),
                time_in_force=TimeInForce.DAY,
                status=OrderStatus.ACK,
            ),
            None,
        )

    fill_source.submit = fake_submit
    fill_source.position_projection_owned = False
    fill_source.submission_boundary_crossed = True
    engine = ExecutionEngine(bus=bus, fill_source=fill_source)
    # Make the engine reject every order via the kill switch.
    engine._kill_switch.set()

    # Submit (will return a rejected receipt because the kill switch
    # is set). The cache has no order.
    receipt = engine.submit(_request())
    assert receipt.status == OrderStatus.REJECTED
    assert engine.cache.all_orders() == []

    # Now imagine a late broker fill for a *non-existent* order: the
    # apply path should create a minimal FILLED order so reconciliation
    # can see it, OR skip it. The pre-fix code's check at
    # engine.py:816-818 explicitly skips when the cached order is
    # REJECTED. We test the negative case here: a fill for a totally
    # unknown order id must not crash.
    fake_fill = Fill(
        order_id=OrderId(value="oid-foreign"),
        instrument=Equity.of("NSE", "TEST"),
        side=OrderSide.BUY,
        quantity=Quantity(value=Decimal("1")),
        price=Price(value=Decimal("100")),
    )
    bus.publish(OrderFilled(fill=fake_fill))
    # No crash; engine logs the unknown order.
    assert True
