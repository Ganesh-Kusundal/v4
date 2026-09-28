"""Order-intake tests: ``ExecutionEngine.submit()`` is the only door (AC-22).

This file used to be "CQRS PlaceOrderCommand tests", and it verified that
publishing a ``PlaceOrderCommand`` on the public bus triggered order
processing. That was a real, working backdoor: the engine subscribed to the
command type, so anything holding a bus reference could put an order in front
of the venue.

What a bus publisher actually skipped is worth recording, because it is
smaller than it looks. Both the command subscription and the plain
``OrderRequest`` subscription converged on ``_process_request`` →
``_run_pipeline``, so the ENGINE-level gates — kill switch, feed readiness,
idempotency, risk, the cash floor — ran for bus publishers exactly as they ran
for ``submit()``. The bypass was in the strategy layer, above the engine:
``PositionSizer`` and ``protective_request`` (the SL/TP legs).

The tests below are kept, not deleted, because each one pinned a *gate*
behaviour that must survive the door closing. They now drive the same gates
through ``submit()`` and additionally assert that the bus is inert, so the
suite would catch the door being re-opened from either side.
"""

from __future__ import annotations

from decimal import Decimal

from tradex_domain import Equity
from tradex_domain.enums import OrderSide, OrderStatus, OrderType
from tradex_domain.events import PlaceOrderCommand
from tradex_domain.execution import OrderRequest
from tradex_domain.value_objects import Price, Quantity
from tradex_execution.engine import ExecutionEngine
from tradex_execution.fill_sources import SimulatedFillSource
from tradex_reactive.bus import ReactiveBus


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


def test_submit_triggers_processing() -> None:
    """``submit()`` places the order and fills it through the fill source."""
    bus = ReactiveBus()
    engine = ExecutionEngine(bus=bus, fill_source=SimulatedFillSource())

    engine.submit(_request())

    orders = engine.cache.all_orders()
    assert len(orders) == 1
    assert orders[0].status is OrderStatus.FILLED
    assert orders[0].instrument.symbol == "RELIANCE"


def test_submit_updates_position() -> None:
    """A submit flows through the full pipeline including the position update."""
    bus = ReactiveBus()
    engine = ExecutionEngine(bus=bus, fill_source=SimulatedFillSource())

    engine.submit(_request())

    pos = engine.cache.get_position(_eq())
    assert pos is not None
    assert pos.quantity.value == 10


def test_multiple_submits_are_all_processed() -> None:
    """Three submits produce three orders — no dedup by accident."""
    bus = ReactiveBus()
    engine = ExecutionEngine(bus=bus, fill_source=SimulatedFillSource())

    for _ in range(3):
        engine.submit(_request())

    assert len(engine.cache.all_orders()) == 3


def test_submit_respects_the_kill_switch() -> None:
    """The kill switch refuses a submit, and the refusal is auditable.

    Note what this test did NOT need: before AC-22 there was a second copy of
    it for the bus path, on the theory that a bus publisher could bypass the
    kill switch. It could not — both doors ran this same gate. One door, one
    test.
    """
    bus = ReactiveBus()
    engine = ExecutionEngine(bus=bus, fill_source=SimulatedFillSource())

    engine.trip_kill_switch(reason="test halt")
    engine.submit(_request())

    # A refused submit must be auditable, but must never reach the fill source.
    orders = engine.cache.all_orders()
    assert len(orders) == 1
    assert orders[0].status is OrderStatus.REJECTED
    assert engine.cache.all_positions() == []


class TestTheClosedDoor:
    """The negative controls for this file: the bus door is shut."""

    def test_a_published_place_order_command_does_nothing(self) -> None:
        """Publishing the command is inert — the AC-22 hole itself.

        Re-adding ``bus.of_type(PlaceOrderCommand).subscribe(...)`` to
        ``_setup_pipeline`` makes this fail, and with it the three gate tests
        above lose their meaning: they would again be testing a second door.
        """
        bus = ReactiveBus()
        engine = ExecutionEngine(bus=bus, fill_source=SimulatedFillSource())

        bus.publish(PlaceOrderCommand(request=_request()))

        assert engine.cache.all_orders() == [], (
            "a PlaceOrderCommand on the bus placed an order; submit() must be "
            "the only path to the venue (AC-22)"
        )
        assert engine.cache.all_positions() == []

    def test_a_published_place_order_command_cannot_beat_the_kill_switch(self) -> None:
        """The sharpest version: a halted session, attacked from the bus.

        Worth its own test because it is the failure that would matter. If the
        bus door were open, a halted session would still take an order off the
        bus — and, per the sizing bypass, one the strategy layer never sized
        or protected. The halt is the last line of defence; this asserts the
        bus cannot walk past it.
        """
        bus = ReactiveBus()
        engine = ExecutionEngine(bus=bus, fill_source=SimulatedFillSource())
        engine.trip_kill_switch(reason="test halt")

        bus.publish(PlaceOrderCommand(request=_request()))

        assert engine.cache.all_orders() == [], (
            "a halted session still placed an order from the bus"
        )
        assert engine.cache.all_positions() == []
