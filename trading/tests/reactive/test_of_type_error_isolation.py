"""Error isolation for ``of_type()`` subscribers — the starvation regression.

``ReactiveBus.subscribe()`` wraps handlers in ``_isolate``, but
``of_type(X)`` hands callers a bare lane Subject that they subscribe to
directly. rx 3.2's ``AutoDetachObserver.on_next`` does NOT catch exceptions,
so before the fix one raising ``of_type`` handler:

  1. propagated the exception out of ``publish()``,
  2. was never disposed, permanently starving every sibling subscriber,
  3. left the Subject's internal RLock held, so plain ``subscribe()``
     consumers were starved too.

The real-world cost was silent and permanent: the execution engine subscribes
via ``of_type`` (``engine.py:285/295/304``) and the durable order store
subscribes ``OrderFilled`` via ``of_type`` (``sqlite_store.py:587``), so a
raising engine handler meant the store never recorded the fill.

These tests import ``tradex_reactive.bus`` directly. The legacy
``tradex_trading.reactive.bus`` shim is a separate module object that
re-exports the *same* ``ReactiveBus`` class object, so behaviour is identical.
"""

from __future__ import annotations

import logging
from typing import Any

from tradex_domain.events import OrderFilled
from tradex_domain.execution import Fill
from tradex_domain.instruments import Equity

from tradex_reactive.bus import ReactiveBus


class _FakeCounter:
    def __init__(self) -> None:
        self.value = 0

    def inc(self) -> None:
        self.value += 1


class _FakeMetrics:
    def __init__(self) -> None:
        self.counters: dict[str, _FakeCounter] = {}

    def counter(self, name: str) -> _FakeCounter:
        if name not in self.counters:
            self.counters[name] = _FakeCounter()
        return self.counters[name]


def _raise_on_next(m: Any) -> None:
    del m
    raise RuntimeError("subscriber boom")


class _Event:
    """Unknown-to-the-bus type: routes to the default lane, so ``of_type``
    falls back to the all-lane Subject."""

    def __init__(self, tag: str) -> None:
        self.tag = tag

    def __repr__(self) -> str:
        return f"_Event({self.tag})"

    def __eq__(self, other: object) -> bool:
        return isinstance(other, _Event) and other.tag == self.tag

    def __hash__(self) -> int:
        return hash((_Event, self.tag))


def _make_fill() -> Fill:
    """An ``OrderFilled`` — a known lane, the engine/store real path."""
    return Fill(
        fill_id="FILL-1",
        order_id="ORD-1",
        instrument=Equity.of("NSE", "RELIANCE"),
        side=None,
        quantity=None,
        price=None,
    )


class TestOfTypeIsolation:
    """A raising ``of_type`` handler must not affect anyone else."""

    def test_raising_of_type_does_not_block_sibling_of_type(self) -> None:
        bus = ReactiveBus()
        received: list[Any] = []
        bus.of_type(_Event).subscribe(_raise_on_next)
        bus.of_type(_Event).subscribe(received.append)
        for i in range(3):
            bus.publish(_Event(str(i)))
        assert received == [_Event("0"), _Event("1"), _Event("2")]

    def test_raising_of_type_does_not_block_plain_subscribe(self) -> None:
        bus = ReactiveBus()
        received: list[Any] = []
        bus.of_type(_Event).subscribe(_raise_on_next)
        bus.subscribe(on_next=received.append)
        for i in range(3):
            bus.publish(_Event(str(i)))
        assert len(received) == 3

    def test_publish_does_not_raise_with_raising_of_type_subscriber(self) -> None:
        bus = ReactiveBus()
        bus.of_type(_Event).subscribe(_raise_on_next)
        bus.publish(_Event("m"))  # must not raise

    def test_healthy_subscriber_not_permanently_starved(self) -> None:
        """THE regression: a healthy sibling must survive later raising publishes.

        Pre-fix the raising observer wedged the Subject, so the healthy
        subscriber received nothing and stayed dead for the life of the bus.

        The starved subscriber is the one registered *after* the raiser,
        which is the ordering that reproduces: rx iterates the Subject's
        observer list in registration order, so a raising observer
        registered first aborts the loop before the sibling gets a turn.
        The healthy-first ordering is covered separately by
        :meth:`test_healthy_first_order_not_permanently_starved` — before
        the fix only one of the two orderings survived, so asserting both
        is what makes this a real regression test.
        """
        bus = ReactiveBus()
        received: list[Any] = []
        bus.of_type(_Event).subscribe(_raise_on_next)  # registers first
        bus.of_type(_Event).subscribe(received.append)

        bus.publish(_Event("first"))
        assert received == [_Event("first")], (
            "a raising sibling must not starve an already-attached subscriber"
        )

        # Subsequent raising publishes must not stop the healthy subscriber.
        for i in range(5):
            bus.publish(_Event(f"m{i}"))
        assert len(received) == 6

    def test_healthy_first_order_not_permanently_starved(self) -> None:
        """The other registration order must stay healthy too."""
        bus = ReactiveBus()
        received: list[Any] = []
        bus.of_type(_Event).subscribe(received.append)  # registers first
        bus.of_type(_Event).subscribe(_raise_on_next)
        bus.publish(_Event("first"))
        for i in range(5):
            bus.publish(_Event(f"m{i}"))
        assert len(received) == 6

    def test_subscriber_errors_counter_increments_for_of_type(self) -> None:
        metrics = _FakeMetrics()
        bus = ReactiveBus(metrics=metrics)
        bus.of_type(_Event).subscribe(_raise_on_next)
        bus.publish(_Event("m1"))
        bus.publish(_Event("m2"))
        assert metrics.counters["bus.messages.subscriber_errors"].value == 2

    def test_of_type_error_is_logged(self, caplog: Any) -> None:
        bus = ReactiveBus()
        bus.of_type(_Event).subscribe(_raise_on_next)
        with caplog.at_level(logging.ERROR, logger="tradex_reactive.bus"):
            bus.publish(_Event("m"))
        assert any("Subscriber error" in r.message for r in caplog.records)

    def test_no_metrics_no_crash(self) -> None:
        bus = ReactiveBus()
        bus.of_type(_Event).subscribe(_raise_on_next)
        bus.publish(_Event("m"))


class TestEngineLaneOfTypeIsolation:
    """Same defect on a known lane: the engine/store ``OrderFilled`` path."""

    def _bus(self) -> tuple[ReactiveBus, Fill]:
        bus = ReactiveBus()
        return bus, _make_fill()

    def test_raising_engine_handler_does_not_starve_fill_store(self) -> None:
        """The durable-store analogue: a raising OrderFilled subscriber must
        not stop a sibling OrderFilled subscriber from recording the fill."""
        bus, fill = self._bus()
        recorded: list[Any] = []
        # engine.py:304 subscribes OrderFilled first, then sqlite_store.py:587.
        bus.of_type(OrderFilled).subscribe(_raise_on_next)
        bus.of_type(OrderFilled).subscribe(recorded.append)
        bus.publish(OrderFilled(fill=fill))
        bus.publish(OrderFilled(fill=fill))
        assert len(recorded) == 2

    def test_raising_of_type_does_not_starve_all_lane_subscribe(self) -> None:
        """Known lane: the all-lane ``subscribe()`` path stays healthy too."""
        bus, fill = self._bus()
        received: list[Any] = []
        bus.of_type(OrderFilled).subscribe(_raise_on_next)
        bus.subscribe(on_next=received.append)
        bus.publish(OrderFilled(fill=fill))
        assert len(received) == 1


class TestOfTypeFallbackPreserved:
    """The all-lane fallback for unknown types must still work."""

    def test_unknown_type_falls_back_to_all_lane_subject(self) -> None:
        bus = ReactiveBus()
        events: list[Any] = []
        bus.of_type(_Event).subscribe(events.append)
        bus.publish(OrderFilled(fill=_make_fill()))  # different type: filtered out
        bus.publish(_Event("kept"))
        assert events == [_Event("kept")]

    def test_unknown_type_subscriber_still_receives_healthy_stream(self) -> None:
        bus = ReactiveBus()
        received: list[Any] = []
        bus.of_type(_Event).subscribe(received.append)
        for i in range(4):
            bus.publish(_Event(str(i)))
        assert len(received) == 4

    def test_of_type_filters_correctly(self) -> None:
        bus = ReactiveBus()
        ints: list[Any] = []
        strs: list[Any] = []
        bus.of_type(int).subscribe(lambda m: ints.append(m))
        bus.of_type(str).subscribe(lambda m: strs.append(m))
        bus.publish(1)
        bus.publish("two")
        bus.publish(3)
        assert ints == [1, 3]
        assert strs == ["two"]

    def test_multiple_of_type_same_type_both_receive(self) -> None:
        bus = ReactiveBus()
        a: list[Any] = []
        b: list[Any] = []
        bus.of_type(int).subscribe(lambda m: a.append(m))
        bus.of_type(int).subscribe(lambda m: b.append(m))
        bus.publish(1)
        bus.publish(2)
        assert a == [1, 2]
        assert b == [1, 2]


class TestStreamAndOperatorEntryPoints:
    """Isolation must hold for every way a subscriber attaches."""

    def test_stream_subscriber_is_isolated(self) -> None:
        bus = ReactiveBus()
        received: list[Any] = []
        bus.stream().subscribe(_raise_on_next)
        bus.stream().subscribe(received.append)
        bus.publish("m1")
        bus.publish("m2")
        assert received == ["m1", "m2"]

    def test_pipelined_of_type_subscriber_is_isolated(self) -> None:
        """``of_type(...).pipe(op)`` is a distinct entry point."""
        from rx import operators as ops

        bus = ReactiveBus()
        received: list[Any] = []
        bus.of_type(_Event).pipe(ops.map(lambda e: e)).subscribe(_raise_on_next)
        bus.of_type(_Event).pipe(ops.map(lambda e: e)).subscribe(received.append)
        bus.publish(_Event("m"))
        assert received == [_Event("m")]

    def test_observer_form_subscribe_is_isolated(self) -> None:
        """``subscribe(Observer)`` rather than the callback form."""
        from rx.core import Observer

        bus = ReactiveBus()
        received: list[Any] = []
        bus.of_type(_Event).subscribe(Observer(on_next=_raise_on_next))
        bus.of_type(_Event).subscribe(Observer(on_next=received.append))
        bus.publish(_Event("m"))
        assert received == [_Event("m")]

    def test_on_completed_still_delivered_through_isolation(self) -> None:
        """The wrapper must forward terminal events, not just on_next."""
        bus = ReactiveBus()
        completed: list[bool] = []
        bus.of_type(_Event).subscribe(on_completed=lambda: completed.append(True))
        bus.dispose()
        assert completed == [True]
