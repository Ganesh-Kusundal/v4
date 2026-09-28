"""Tests for the reconciliation monitor (runtime.reconciliation_monitor).

Every test here is a negative control: each one fails when the behaviour it
names is removed. The behaviours are the load-bearing ones for lane 5e — the
monitor owns a bounded daemon thread, never raises, reports adoption metrics,
and stops cleanly — and the adoption safety itself is pinned in
``trading/tests/interface/test_safety_adoption.py``.

The fakes below mirror the real :class:`tradex_interfaces.safety` types by
hand rather than importing them, because these tests are pinned to the
*runtime* contract (what the monitor does with an outcome), not the safety
contract (what the outcome means). Importing the real types here would make
these tests pass against any shape the safety module chose, which is not what
they are for.
"""

from __future__ import annotations

import threading
import time
from typing import Any

import pytest
from tradex_runtime.reconciliation_monitor import (
    RECONCILIATION_INTERVAL_SECONDS,
    ReconciliationMonitor,
)


class _FakeRegistry:
    """Minimal ``MetricsRegistry`` shape, recording calls by name."""

    def __init__(self) -> None:
        self.counters: dict[str, list[tuple[float, dict[str, object]]]] = {}
        self.gauges: dict[str, list[float]] = {}
        self.counter_names: list[str] = []
        self.gauge_names: list[str] = []

    def counter(self, name: str, help_text: str = "") -> _Counter:
        self.counter_names.append(name)
        return _Counter(self.counters.setdefault(name, []))

    def gauge(self, name: str, help_text: str = "") -> _Gauge:
        self.gauge_names.append(name)
        return _Gauge(self.gauges.setdefault(name, []))

    def amount(self, name: str) -> float:
        return sum(a for a, _ in self.counters.get(name, []))

    def labels(self, name: str) -> list[dict[str, object]]:
        return [labels for _, labels in self.counters.get(name, [])]


class _Counter:
    def __init__(self, sink: list[tuple[float, dict[str, object]]]) -> None:
        self._sink = sink

    def inc(self, amount: float = 1, **labels: object) -> None:
        self._sink.append((float(amount), dict(labels)))


class _Gauge:
    def __init__(self, sink: list[float]) -> None:
        self._sink = sink

    def set(self, value: float) -> None:
        self._sink.append(float(value))


class _VenueAdoption:
    """The shape of ``VenueAdoption``, including the derived counters."""

    def __init__(
        self,
        *,
        adopted: int = 0,
        cleared: int = 0,
        skipped: dict[str, str] | None = None,
    ) -> None:
        self.positions = {f"NSE:REL{i}": object() for i in range(adopted)}
        self.cleared = {f"NSE:TCS{i}": object() for i in range(cleared)}
        self.skipped = dict(skipped or {})
        self.retained: dict[str, object] = {}
        self.previous: dict[str, object] = {}

    @property
    def adopted_count(self) -> int:
        return len(self.positions)

    @property
    def is_empty(self) -> bool:
        return not (self.positions or self.cleared or self.skipped)

    def as_log_rows(self) -> list[dict[str, object]]:
        return [{"instrument_id": iid, "action": "adopted"} for iid in self.positions]


class _VenueRead:
    """The shape of ``VenueRead`` — the fields the monitor reads."""

    def __init__(
        self,
        *,
        failed: bool = False,
        partial: bool = False,
        read_at: Any = None,
    ) -> None:
        self.positions: tuple[object, ...] = (object(),) if not failed else ()
        self.unreadable_instruments: tuple[str, ...] = ("NSE:BAD",) if partial else ()
        self.failed = failed
        self.failure = "boom" if failed else ""
        self.error: BaseException | None = RuntimeError("boom") if failed else None
        self.read_at = read_at

    @property
    def partial(self) -> bool:
        return bool(self.unreadable_instruments)

    @property
    def ok(self) -> bool:
        return not self.failed and not self.partial

    def as_log_dict(self) -> dict[str, object]:
        return {"failed": self.failed, "partial": self.partial}


class _AdoptionOutcome:
    """The shape of ``AdoptionOutcome``."""

    def __init__(
        self,
        *,
        applied: bool,
        refusal: str = "",
        plan: _VenueAdoption | None = None,
        read: _VenueRead | None = None,
        read_failed: bool = False,
    ) -> None:
        self.adoption = plan if plan is not None else _VenueAdoption()
        self.applied = applied
        self.refusal = refusal
        self.read = read if read is not None else _VenueRead(failed=read_failed)

    @property
    def adopted_count(self) -> int:
        return self.adoption.adopted_count


def _outcome(
    *,
    applied: bool = True,
    adopted: int = 0,
    cleared: int = 0,
    skipped: dict[str, str] | None = None,
    refusal: str = "",
    read_failed: bool = False,
    read_partial: bool = False,
) -> _AdoptionOutcome:
    return _AdoptionOutcome(
        applied=applied,
        refusal=refusal,
        plan=_VenueAdoption(adopted=adopted, cleared=cleared, skipped=skipped),
        read=_VenueRead(failed=read_failed, partial=read_partial),
    )


def _wait_until(predicate, timeout: float = 2.0) -> bool:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if predicate():
            return True
        time.sleep(0.01)
    return predicate()


def _monitor(**kwargs) -> ReconciliationMonitor:
    """A monitor whose missing seams default to no-ops, like the real default."""
    kwargs.setdefault("reconcile", lambda: ())
    kwargs.setdefault("adopt", lambda: _outcome(adopted=0))
    return ReconciliationMonitor(**kwargs)


# -- lifecycle: the feed-monitor contract, verbatim ----------------------------


def test_start_spawns_exactly_one_daemon_thread() -> None:
    """One bounded daemon thread, and a second ``start()`` does not add another.

    Counted while running, not after ``stop()``: the threads share one stop event,
    so a stray thread exits soon after the one ``stop()`` joins, and checking late
    would not see it.
    """
    monitor = _monitor(interval_seconds=10.0)
    monitor.start()
    try:
        monitor.start()
        monitor.start()
        alive = [
            t for t in threading.enumerate() if t.name == "reconciliation-monitor"
        ]
        # The guard is "never spawn a second timer": the guard is in ``start``,
        # which returns early while the first thread is still alive.
        assert len(alive) == 1
        assert alive[0].daemon is True
    finally:
        monitor.stop()
    assert monitor.running is False
    assert [t for t in threading.enumerate() if t.name == "reconciliation-monitor"] == []


def test_thread_is_daemon_and_named() -> None:
    """Bounded and named, so a leak is visible in a thread dump."""
    monitor = _monitor(interval_seconds=10.0)
    monitor.start()
    try:
        alive = [t for t in threading.enumerate() if t.name == "reconciliation-monitor"]
        assert len(alive) == 1
        assert alive[0].daemon is True
    finally:
        monitor.stop()


def test_stop_is_idempotent_and_joins() -> None:
    monitor = _monitor(interval_seconds=0.02)
    monitor.start()
    monitor.stop()
    monitor.stop()
    assert monitor.running is False


def test_stop_without_start_is_safe() -> None:
    monitor = _monitor()
    monitor.stop()
    assert monitor.running is False


def test_stop_clears_the_thread_reference() -> None:
    """A stopped monitor holds no thread, so ``start`` cannot reuse a corpse."""
    monitor = _monitor(interval_seconds=10.0)
    monitor.start()
    monitor.stop()
    assert monitor._thread is None
    assert monitor.running is False


def test_context_manager_starts_and_stops() -> None:
    with _monitor(interval_seconds=10.0) as monitor:
        assert monitor.running is True
    assert monitor.running is False


def test_restart_after_stop_runs_again() -> None:
    """A stopped monitor can be restarted, and still only owns one thread."""
    calls: list[int] = []
    monitor = _monitor(
        adopt=lambda: (calls.append(1), _outcome(adopted=1))[-1],
        interval_seconds=0.02,
    )
    try:
        monitor.start()
        assert _wait_until(lambda: len(calls) >= 1)
        monitor.stop()
        first = len(calls)
        monitor.start()
        assert _wait_until(lambda: len(calls) >= first + 1)
    finally:
        monitor.stop()


def test_start_replaces_a_dead_thread() -> None:
    """A thread that died must not refuse the restart.

    The guard is ``_thread is None``, not ``running``: a corpse is still a thread,
    and guarding on liveness would leave a monitor that crashed once unrestartable
    for the rest of the session. The restart is what makes a monitor that died once
    recoverable rather than silently dead for the rest of the session.

    The thread handle is not compared, because CPython recycles a joined
    :class:`threading.Thread` object wholesale — ``_started`` is a fresh event but
    the object is reused, so identity is meaningless after a join. The observable
    contract is that the poll loop runs again.
    """
    polls: list[int] = []
    monitor = _monitor(
        reconcile=lambda: (polls.append(1), ()),
        interval_seconds=0.02,
    )
    monitor.start()
    first = monitor._thread
    assert first is not None
    assert _wait_until(lambda: len(polls) >= 1)
    monitor.stop()
    assert first.is_alive() is False
    assert monitor.running is False

    # Simulate the crash state: a dead thread reference, still held. This is what
    # a thread that died on an unhandled error leaves behind.
    monitor._thread = first
    monitor._stop.clear()
    before = len(polls)
    monitor.start()
    try:
        assert _wait_until(lambda: len(polls) >= before + 1)
    finally:
        monitor.stop()
    assert monitor.running is False


def test_negative_interval_is_rejected() -> None:
    with pytest.raises(ValueError, match="positive"):
        ReconciliationMonitor(reconcile=lambda: (), interval_seconds=0)


def test_default_interval_is_30s() -> None:
    assert RECONCILIATION_INTERVAL_SECONDS == 30.0
    assert _monitor().interval_seconds == 30.0


# -- never raises --------------------------------------------------------------


def test_poll_once_survives_a_raising_reconcile() -> None:
    """A comparison that dies must not take the monitor with it."""

    def boom() -> list[object]:
        raise RuntimeError("engine gone")

    monitor = ReconciliationMonitor(reconcile=boom, adopt=None)
    drifts, adoption = monitor.poll_once()
    assert drifts == ()
    assert adoption is None
    assert monitor.last_drifts == ()


def test_poll_once_survives_a_raising_adopt() -> None:
    """An adoption that dies is logged, not propagated."""

    def boom() -> object:
        raise RuntimeError("venue gone")

    monitor = ReconciliationMonitor(reconcile=None, adopt=boom)
    drifts, adoption = monitor.poll_once()
    assert drifts == ()
    assert adoption is None
    assert monitor.last_adoption is None


def test_poll_once_survives_a_raising_metric_registry() -> None:
    """A metrics backend that dies must not lose the reconciliation itself.

    This is the one defensive belt that is easy to write the wrong way round:
    swallowing a metric failure is a silent hole, but letting it escape costs
    the whole poll. The poll wins, and the failure is logged.
    """

    class Broken:
        def counter(self, *args, **kwargs):
            raise RuntimeError("metrics gone")

        def gauge(self, *args, **kwargs):
            raise RuntimeError("metrics gone")

    monitor = ReconciliationMonitor(
        reconcile=lambda: (),
        adopt=lambda: _outcome(adopted=1),
        metrics=Broken(),
    )
    drifts, adoption = monitor.poll_once()
    assert drifts == ()
    assert adoption is not None
    assert monitor.last_adoption is adoption
    assert monitor.adopted_count == 1


def test_poll_once_survives_an_outcome_that_cannot_be_logged() -> None:
    """An outcome whose logging raises must not cost the poll.

    The log is the audit trail, not the mutation: it is written *after* the book
    was changed, so losing it is a reporting hole rather than a safety hole, and
    the next poll still has to run.
    """

    class _BrokenOutcome:
        applied = True
        adoption = None
        adopted_count = 1
        read = None

        @property
        def refusal(self) -> str:
            raise RuntimeError("refusal unreadable")

    monitor = ReconciliationMonitor(
        reconcile=lambda: (), adopt=lambda: _BrokenOutcome(),
    )
    drifts, adoption = monitor.poll_once()
    assert drifts == ()
    assert adoption is not None
    assert monitor.adopted_count == 1
    assert monitor.last_adoption is adoption


def test_poll_once_survives_a_refusal_that_cannot_be_read() -> None:
    """A refusal whose own fields raise is still not a raise."""

    class _BrokenRefusal:
        applied = False
        adoption = None
        adopted_count = 4
        read = _VenueRead()

        @property
        def refusal(self) -> str:
            raise RuntimeError("refusal unreadable")

    monitor = ReconciliationMonitor(
        reconcile=lambda: (), adopt=lambda: _BrokenRefusal(),
    )
    drifts, adoption = monitor.poll_once()
    assert drifts == ()
    assert adoption is not None
    assert monitor.adopted_count == 4


def test_poll_once_survives_a_raising_gauge_set() -> None:
    class BrokenGauge:
        def counter(self, *args, **kwargs):
            return _Counter([])

        def gauge(self, *args, **kwargs):
            raise RuntimeError("gauge gone")

    monitor = ReconciliationMonitor(
        reconcile=lambda: (),
        adopt=lambda: _outcome(adopted=1),
        metrics=BrokenGauge(),
    )
    drifts, adoption = monitor.poll_once()
    assert adoption is not None
    assert monitor.adopted_count == 1


def test_no_seams_poll_once_returns_empties() -> None:
    monitor = ReconciliationMonitor()
    drifts, adoption = monitor.poll_once()
    assert drifts == ()
    assert adoption is None
    assert monitor.adopted_count == 0
    assert monitor.last_drifts == ()
    assert monitor.last_adoption is None


# -- seam results are recorded -------------------------------------------------


def test_poll_once_records_drifts_and_adoption() -> None:
    drift = object()
    monitor = ReconciliationMonitor(
        reconcile=lambda: [drift],
        adopt=lambda: _outcome(adopted=2, cleared=1),
    )
    drifts, adoption = monitor.poll_once()
    assert tuple(drifts) == (drift,)
    assert adoption is not None
    assert monitor.last_drifts == (drift,)
    assert monitor.last_adoption is adoption
    assert monitor.adopted_count == 2


def test_adopted_count_reads_the_plan_not_the_applied_flag() -> None:
    """A refused adoption still reports how many it *would* have written.

    An operator needs the size of a refused correction to judge whether the
    refusal is costing them money; reporting 0 because the write was refused
    hides the only interesting number.
    """
    monitor = ReconciliationMonitor(
        reconcile=lambda: (),
        adopt=lambda: _outcome(applied=False, refusal="venue_read_partial", adopted=5),
    )
    monitor.poll_once()
    assert monitor.adopted_count == 5


def test_adopted_count_is_zero_when_no_adoption_has_run() -> None:
    """No adoption has run yet, so the count is 0 rather than a raise."""
    monitor = ReconciliationMonitor(
        reconcile=lambda: (),
        adopt=lambda: _outcome(adopted=3),
    )
    assert monitor.adopted_count == 0


def test_adopted_count_is_zero_after_a_raising_adopt_seam() -> None:
    """A seam that raised leaves no outcome, so the count reads 0 not an error."""

    def boom() -> object:
        raise RuntimeError("venue gone")

    monitor = ReconciliationMonitor(reconcile=lambda: (), adopt=boom)
    monitor.poll_once()
    assert monitor.last_adoption is None
    assert monitor.adopted_count == 0


def test_adopted_count_is_zero_when_the_outcome_reports_none() -> None:
    """An outcome that reports nothing is 0, never a raise.

    ``adopted_count`` is read off the outcome by attribute, so an outcome without
    the attribute must degrade rather than explode.
    """
    monitor = ReconciliationMonitor(
        reconcile=lambda: (),
        adopt=lambda: object(),
    )
    monitor.poll_once()
    assert monitor.adopted_count == 0


def test_adopted_count_reads_the_outcomes_own_count() -> None:
    """The outcome's ``adopted_count`` is authoritative, not a derived re-count."""

    class _OddOutcome:
        applied = True
        adopted_count = 11
        read = _VenueRead()
        adoption = _VenueAdoption()

    monitor = ReconciliationMonitor(
        reconcile=lambda: (), adopt=lambda: _OddOutcome(),
    )
    monitor.poll_once()
    assert monitor.adopted_count == 11


def test_adopted_count_is_zero_when_the_plan_is_absent() -> None:
    monitor = ReconciliationMonitor(reconcile=lambda: (), adopt=lambda: object())
    monitor.poll_once()
    assert monitor.adopted_count == 0


def test_adopted_count_reads_the_outcomes_own_count_after_a_plan_with_no_count() -> None:
    """The outcome wins over the plan when both are present."""

    class _PlanNoCount:
        positions: dict[str, object] = {}
        cleared: dict[str, object] = {}
        skipped: dict[str, str] = {}
        retained: dict[str, object] = {}
        is_empty = True

        def as_log_rows(self) -> list[dict[str, object]]:
            return []

    class _OutcomeWithCount:
        applied = True
        adopted_count = 17
        adoption = _PlanNoCount()
        read = _VenueRead()

    monitor = ReconciliationMonitor(
        reconcile=lambda: (), adopt=lambda: _OutcomeWithCount(),
    )
    monitor.poll_once()
    assert monitor.adopted_count == 17


def test_last_adoption_is_replaced_each_poll() -> None:
    first = _outcome(adopted=1)
    second = _outcome(adopted=9)
    outcomes = iter([first, second])
    monitor = ReconciliationMonitor(reconcile=lambda: (), adopt=lambda: next(outcomes))
    monitor.poll_once()
    assert monitor.last_adoption is first
    assert monitor.adopted_count == 1
    monitor.poll_once()
    assert monitor.last_adoption is second
    assert monitor.adopted_count == 9


# -- metrics --------------------------------------------------------------------


def test_applied_adoption_emits_adopted_cleared_and_gauge() -> None:
    registry = _FakeRegistry()
    monitor = ReconciliationMonitor(
        reconcile=lambda: (),
        adopt=lambda: _outcome(adopted=2, cleared=1),
        metrics=registry,
    )
    monitor.poll_once()
    assert registry.amount("venue_adoption_adopted_total") == 2.0
    assert registry.amount("venue_adoption_cleared_total") == 1.0
    assert registry.gauges["venue_adoption_last_adopted"] == [2.0]


def test_refused_adoption_emits_refused_with_reason() -> None:
    registry = _FakeRegistry()
    monitor = ReconciliationMonitor(
        reconcile=lambda: (),
        adopt=lambda: _outcome(
            applied=False,
            refusal="venue_read_partial",
            skipped={"NSE:X": "unsettled_fill"},
        ),
        metrics=registry,
    )
    monitor.poll_once()
    assert registry.amount("venue_adoption_refused_total") == 1.0
    assert registry.labels("venue_adoption_refused_total") == [
        {"reason": "venue_read_partial"},
    ]
    assert registry.amount("venue_adoption_adopted_total") == 0.0
    assert registry.amount("venue_adoption_cleared_total") == 0.0
    assert "venue_adoption_last_adopted" not in registry.gauges


def test_failed_read_emits_read_failed_counter() -> None:
    registry = _FakeRegistry()
    monitor = ReconciliationMonitor(
        reconcile=lambda: (),
        adopt=lambda: _outcome(applied=False, refusal="venue_read_failed",
                               read_failed=True),
        metrics=registry,
    )
    monitor.poll_once()
    assert registry.amount("venue_read_failed_total") == 1.0
    assert registry.amount("venue_adoption_refused_total") == 1.0


def test_a_clean_read_emits_no_read_failed_counter() -> None:
    registry = _FakeRegistry()
    monitor = ReconciliationMonitor(
        reconcile=lambda: (),
        adopt=lambda: _outcome(adopted=1),
        metrics=registry,
    )
    monitor.poll_once()
    assert "venue_read_failed_total" not in registry.counters


def test_skipped_reasons_are_labelled_per_reason() -> None:
    registry = _FakeRegistry()
    monitor = ReconciliationMonitor(
        reconcile=lambda: (),
        adopt=lambda: _outcome(
            adopted=1,
            skipped={
                "NSE:A": "unsettled_fill",
                "NSE:B": "venue_omitted",
                "NSE:C": "unsettled_fill",
            },
        ),
        metrics=registry,
    )
    monitor.poll_once()
    by_reason: dict[str, float] = {}
    for amount, labels in registry.counters["venue_adoption_skipped_total"]:
        by_reason[str(labels["reason"])] = amount
    assert by_reason == {"unsettled_fill": 2.0, "venue_omitted": 1.0}


def test_a_refused_adoption_still_reports_what_it_would_skip() -> None:
    """A refusal that hides its reasons is a refusal an operator cannot clear."""
    registry = _FakeRegistry()
    monitor = ReconciliationMonitor(
        reconcile=lambda: (),
        adopt=lambda: _outcome(
            applied=False,
            refusal="venue_read_partial",
            skipped={"NSE:A": "venue_omitted"},
        ),
        metrics=registry,
    )
    monitor.poll_once()
    assert registry.amount("venue_adoption_skipped_total") == 1.0
    assert registry.labels("venue_adoption_skipped_total") == [
        {"reason": "venue_omitted"},
    ]


def test_an_empty_adoption_emits_zeroes_not_silence() -> None:
    """An applied adoption with nothing to change still emits, at zero.

    A zero is a reading ("the book agreed with the venue") where an absent
    counter is not one; an operator's graph should show the correction happened
    and found nothing.
    """
    registry = _FakeRegistry()
    monitor = ReconciliationMonitor(
        reconcile=lambda: (),
        adopt=lambda: _outcome(adopted=0, cleared=0),
        metrics=registry,
    )
    monitor.poll_once()
    assert registry.amount("venue_adoption_adopted_total") == 0.0
    assert registry.amount("venue_adoption_cleared_total") == 0.0
    assert registry.gauges["venue_adoption_last_adopted"] == [0.0]
    assert registry.amount("venue_adoption_refused_total") == 0.0
    assert "venue_read_failed_total" not in registry.counters


def test_metrics_are_skipped_when_registry_is_absent() -> None:
    monitor = ReconciliationMonitor(
        reconcile=lambda: (),
        adopt=lambda: _outcome(adopted=1),
    )
    drifts, adoption = monitor.poll_once()
    assert drifts == ()
    assert adoption is not None


def test_metrics_accumulate_across_polls() -> None:
    registry = _FakeRegistry()
    monitor = ReconciliationMonitor(
        reconcile=lambda: (),
        adopt=lambda: _outcome(adopted=2),
        metrics=registry,
    )
    monitor.poll_once()
    monitor.poll_once()
    assert registry.amount("venue_adoption_adopted_total") == 4.0
    assert registry.gauges["venue_adoption_last_adopted"] == [2.0, 2.0]


# -- thread drives the poll ----------------------------------------------------


def test_owned_thread_calls_both_seams_repeatedly() -> None:
    reconcile_calls: list[int] = []
    adopt_calls: list[int] = []
    monitor = ReconciliationMonitor(
        reconcile=lambda: (reconcile_calls.append(1), ()),
        adopt=lambda: (adopt_calls.append(1), _outcome(adopted=0))[-1],
        interval_seconds=0.02,
    )
    monitor.start()
    try:
        assert _wait_until(lambda: len(adopt_calls) >= 3)
    finally:
        monitor.stop()
    assert len(reconcile_calls) >= 3
    assert len(adopt_calls) >= 3


def test_owned_thread_survives_a_raising_poll() -> None:
    """The defensive belt: a poll that raises must not stop the thread."""
    calls: list[int] = []
    state = {"n": 0}

    def flaky() -> _AdoptionOutcome:
        state["n"] += 1
        if state["n"] == 1:
            raise RuntimeError("first poll dies")
        calls.append(1)
        return _outcome(adopted=0)

    monitor = ReconciliationMonitor(
        reconcile=lambda: (),
        adopt=flaky,
        interval_seconds=0.02,
    )
    monitor.start()
    try:
        assert _wait_until(lambda: len(calls) >= 2)
    finally:
        monitor.stop()


def test_owned_thread_survives_a_raising_metrics_registry() -> None:
    """The same belt, from inside the thread: metrics must not kill the loop."""

    class Broken:
        def counter(self, *args, **kwargs):
            raise RuntimeError("metrics gone")

        def gauge(self, *args, **kwargs):
            raise RuntimeError("metrics gone")

    calls: list[int] = []
    monitor = ReconciliationMonitor(
        reconcile=lambda: (),
        adopt=lambda: (calls.append(1), _outcome(adopted=1))[-1],
        metrics=Broken(),
        interval_seconds=0.02,
    )
    monitor.start()
    try:
        assert _wait_until(lambda: len(calls) >= 2)
    finally:
        monitor.stop()


def test_stop_terminates_a_live_poll_loop() -> None:
    monitor = _monitor(interval_seconds=10.0)
    monitor.start()
    time.sleep(0.05)
    monitor.stop(timeout_seconds=2.0)
    assert monitor.running is False


def test_stop_is_signalled_even_mid_sleep() -> None:
    """``stop`` interrupts the interval wait, not the full period."""
    monitor = _monitor(interval_seconds=5.0)
    monitor.start()
    deadline = time.monotonic() + 2.0
    monitor.stop(timeout_seconds=2.0)
    assert monitor.running is False
    assert time.monotonic() < deadline
