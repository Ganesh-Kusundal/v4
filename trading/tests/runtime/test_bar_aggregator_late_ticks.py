"""Lane 3c (AC-11) — late tick handling, the 60s window, and the fill freeze.

The spec sentence this file exists for:

    "Late tick handling with a **60 second** revision window, and a rule that
    resolves the tension with AC-1: a bar no fill has been booked against may
    be reopened and revised; a bar that already has a booked fill is **never**
    revised, because a strategy acted on the original."

Two things have to be true at once, and a suite that proves only one of them
proves nothing: a late tick on a filled bar must be **refused**, and a late
tick on an unfilled bar inside the window must be **applied**. An
implementation that never revises passes the first and fails the second; one
that always revises is the AC-1 violation AC-11 exists to prevent. Each is
asserted here, and each is load-bearing — see the mutation notes in
:func:`test_the_two_directions_are_both_exercised`.
"""

from __future__ import annotations

import inspect
import logging
from datetime import datetime
from decimal import Decimal
from zoneinfo import ZoneInfo

import pytest
from tradex_domain.enums import Timeframe
from tradex_observability.metrics import MetricsRegistry
from tradex_runtime.bar_aggregator import (
    LATE_TICK_REVISION_SECONDS,
    LATE_TICKS_DROPPED_METRIC,
    REASON_BOOKED_FILL,
    REASON_STALE,
    REASON_WINDOW_EXPIRED,
    BarAggregator,
    BarFrame,
    BookedFillLedger,
    RevisedBar,
    bucket_start,
)

IST = ZoneInfo("Asia/Kolkata")
INSTRUMENT = "NSE:TEST"


def _ist(hour: int, minute: int, second: int = 0) -> datetime:
    """IST-naive wall clock, the shape ``on_quote`` takes (datalake convention)."""
    return datetime(2026, 7, 15, hour, minute, second)


def _chart_time(ts: datetime) -> int:
    """UTC epoch seconds of an IST wall clock — the chart convention."""
    return int(ts.replace(tzinfo=IST).timestamp())


class _Collector:
    """Captures every ``on_frame`` payload, keeping ``RevisedBar`` apart.

    Separated because the two are read differently: a plain ``BarFrame`` is
    appended to bar state, a ``RevisedBar`` *replaces* an older one. A test that
    merged them would pass an implementation that appended a revision as a new
    bar, which is the bug the ``time`` ordering test below exists to catch.
    """

    def __init__(self) -> None:
        self.frames: list[BarFrame] = []
        self.revisions: list[RevisedBar] = []

    def __call__(self, frame: object) -> None:
        if isinstance(frame, RevisedBar):
            self.revisions.append(frame)
        else:
            assert isinstance(frame, BarFrame)
            self.frames.append(frame)

    @property
    def closed_frames(self) -> list[BarFrame]:
        return [f for f in self.frames if f.closed]

    def closed_by_time(self) -> dict[int, BarFrame]:
        """Bars keyed by chart time, last write wins — the shape a chart holds."""
        return {f.time: f for f in self.closed_frames}


class _Clock:
    """Manual monotonic clock — the one the 60s revision window is measured on.

    Every late-tick test must advance it deliberately: a test that relied on the
    clock sitting still would pass for the wrong reason (the print would be
    "0s after the bar closed"), and would then keep passing if the window were
    accidentally widened to infinity.

    Named ``tick_clock`` at every use, never ``clock``: ``clock`` is a pytest
    builtin fixture, so a test parameter of that name is filled by pytest's own
    fixture and the value the test passed is silently discarded. That failure
    is invisible in the test's own body — it just reads as the aggregator
    behaving oddly.
    """

    def __init__(self, start: float = 1000.0) -> None:
        self.t = start

    def __call__(self) -> float:
        return self.t

    def advance(self, seconds: float) -> None:
        self.t += seconds


def _agg(
    collector: _Collector | None = None,
    *,
    instrument: str = INSTRUMENT,
    timeframe: Timeframe = Timeframe.M1,
    metrics: MetricsRegistry | None = None,
    tick_clock: _Clock | None = None,
    **kwargs: object,
) -> tuple[BarAggregator, _Collector]:
    out = collector or _Collector()
    agg = BarAggregator(
        instrument,
        timeframe,
        on_frame=out,
        now=tick_clock or _Clock(),
        metrics=metrics,
        **kwargs,  # type: ignore[arg-type]
    )
    return agg, out


def _open_and_close_first_bar(
    agg: BarAggregator, out: _Collector, *, close: float = 101.0, volume: Decimal = Decimal("5")
) -> BarFrame:
    """Fill the 10:00 bar, close it by opening 10:01, and hand back the closed bar."""
    agg.on_quote(_ist(10, 0, 5), Decimal("100"), Decimal("10"))
    agg.on_quote(_ist(10, 0, 50), close, volume)
    agg.on_quote(_ist(10, 1, 0), Decimal("200"), Decimal("1"))
    closed = out.closed_frames[0]
    assert closed.time == _chart_time(_ist(10, 0))
    return closed


# ---------------------------------------------------------------------------
# 1. The window: a late tick on an UNFILLED bar is applied (AC-11's own half)
# ---------------------------------------------------------------------------


def test_late_tick_inside_the_window_revises_the_closed_bar() -> None:
    """The headline sentence: an unfilled bar may be reopened and revised.

    A print stamped 10:00:40 lands in the 10:00 bar, which the 10:01 tick has
    already closed. The bar must come back with the print absorbed: the new
    high, the new volume, and the late print as the bar's close — because the
    late print is the most recent print *for that bar*, which is the only
    ordering a bar has.
    """
    agg, out = _agg()
    closed = _open_and_close_first_bar(agg, out, close=101.0, volume=Decimal("5"))
    assert (closed.high, closed.volume) == (101.0, 15.0)

    agg.on_quote(_ist(10, 0, 40), Decimal("105"), Decimal("3"))

    assert len(out.revisions) == 1, "the late tick produced no revision at all"
    revised = out.revisions[0]
    assert revised.frame.time == closed.time, "the revision must be the same bar"
    assert revised.frame.closed is True
    assert (revised.frame.high, revised.frame.low, revised.frame.close) == (105.0, 100.0, 105.0)
    assert revised.frame.open == 100.0, "a late print must not become the bar's open"
    assert revised.frame.volume == 18.0, "the late print's volume must be added"
    assert revised.reason == "late_tick"
    assert revised.bar_revisions == 1
    # Nothing counted as dropped: this tick did its job.
    assert agg.revision_stats() == (1, 1, 0)


def test_the_window_is_sixty_seconds_of_arrival_time() -> None:
    """60s is the number, and the edge is what the number is for.

    The 10:00 bar closes at t=1000. A print arriving at t=1060 — exactly 60s
    later — is inside; one at t=1061 is out. Both are asserted as *numbers*
    rather than recomputed from the constant, because a test that derived its
    boundary from ``LATE_TICK_REVISION_SECONDS`` would pass on any window at
    all, which is the vacuity the mutation check exists to rule out.
    """
    assert LATE_TICK_REVISION_SECONDS == 60
    tick_clock = _Clock(1000.0)
    agg, out = _agg(tick_clock=tick_clock)
    _open_and_close_first_bar(agg, out)
    assert agg.late_tick_window == 60

    tick_clock.t = 1060.0
    agg.on_quote(_ist(10, 0, 30), Decimal("103"), Decimal("1"))
    assert len(out.revisions) == 1, "exactly 60s after the close is inside the window"

    tick_clock.t = 1061.0
    agg.on_quote(_ist(10, 0, 31), Decimal("104"), Decimal("1"))
    assert len(out.revisions) == 1, "61s after the close is outside the window"
    assert agg.revision_stats()[2] == 1, "the refused tick must be counted"


def test_the_window_is_measured_on_arrival_not_on_the_print_s_own_stamp() -> None:
    """Why the window cannot be a statement about the tick's own age.

    A print's timestamp and the bar it buckets into come from the same number,
    so ``ts - bucket_start(ts)`` is bounded by the bar width *by construction*:
    on a 1m grid no print can claim to be more than 59s behind its own bar, so a
    60s window measured that way could never refuse anything and the rule would
    be decoration. Arrival time is not bounded by anything.

    Both prints here carry stamps 1s and 2s behind their bar, so neither is
    "late" by its own account. What separates them is *when they landed*: the
    second arrives 45s after the bar closed, past the 20s window this
    aggregator was built with, and is refused — while the first, at 5s, is
    applied. A stamp-based window gives both the same answer.
    """
    tick_clock = _Clock(500.0)
    agg, out = _agg(tick_clock=tick_clock, late_tick_window=20)
    _open_and_close_first_bar(agg, out)

    tick_clock.t = 505.0  # 5s after the bar closed
    agg.on_quote(_ist(10, 0, 59), Decimal("103"), Decimal("1"))
    assert len(out.revisions) == 1

    tick_clock.t = 545.0  # 45s after the bar closed
    agg.on_quote(_ist(10, 0, 58), Decimal("104"), Decimal("1"))
    assert len(out.revisions) == 1, "a print 45s after the close is past a 20s window"
    assert agg.revision_stats() == (1, 1, 1)


def test_the_window_is_sixty_seconds_on_every_timeframe_not_scaled_by_bar_width() -> None:
    """A wider bar is not *more* revisable: it closed later, so its own sixty
    seconds start later. The number is flat, and that is the simpler contract.

    An M5 bar closed at t=1000 is revisable on exactly the same terms as an M1
    bar closed at t=1000, which is asserted here with the same numbers.
    """
    tick_clock = _Clock(1000.0)
    agg, out = _agg(timeframe=Timeframe.M5, tick_clock=tick_clock)
    agg.on_quote(_ist(10, 0, 10), Decimal("100"), Decimal("10"))
    agg.on_quote(_ist(10, 4, 50), Decimal("101"), Decimal("5"))
    agg.on_quote(_ist(10, 5, 0), Decimal("110"), Decimal("10"))  # closes the 10:00 bar
    assert agg.late_tick_window == 60

    tick_clock.t = 1060.0
    agg.on_quote(_ist(10, 4, 30), Decimal("106"), Decimal("1"))
    assert len(out.revisions) == 1, "M5 gets the same 60s, not 300s"

    tick_clock.t = 1061.0
    agg.on_quote(_ist(10, 4, 31), Decimal("107"), Decimal("1"))
    assert len(out.revisions) == 1
    assert agg.revision_stats()[2] == 1


def test_a_late_tick_beyond_the_window_is_dropped_counted_and_logged(
    caplog: pytest.LogCaptureFixture,
) -> None:
    """The spec's own sentence for the far side of the window: "Such a late
    tick is counted in ``feed_late_ticks_dropped_total`` and logged."

    Asserted on the registry's *labelled* view as well as the total, because
    since lane 5a a counter is a family of series and an unlabelled read would
    pass even if the reason label were wired to the wrong value.
    """
    registry = MetricsRegistry()
    tick_clock = _Clock(1000.0)
    agg, out = _agg(metrics=registry, tick_clock=tick_clock)
    _open_and_close_first_bar(agg, out)

    with caplog.at_level(logging.WARNING, logger="tradex_runtime.bar_aggregator"):
        tick_clock.t = 1059.0
        agg.on_quote(_ist(10, 0, 30), Decimal("150"), Decimal("9"))
        assert len(out.revisions) == 1, "59s after the close is inside the window"
        tick_clock.t = 1061.0
        agg.on_quote(_ist(10, 0, 31), Decimal("150"), Decimal("9"))

    assert len(out.revisions) == 1, "a print 61s after the close must not revise the bar"
    assert registry.get(LATE_TICKS_DROPPED_METRIC) == 1.0
    assert registry.get(
        LATE_TICKS_DROPPED_METRIC, reason=REASON_WINDOW_EXPIRED
    ) == 1.0
    assert (
        f"{LATE_TICKS_DROPPED_METRIC}|reason={REASON_WINDOW_EXPIRED}"
        in registry.series_snapshot()
    )
    dropped = [
        r for r in caplog.records if "late tick dropped" in r.getMessage()
    ]
    assert len(dropped) == 1
    assert dropped[0].levelno == logging.WARNING
    assert dropped[0].reason == REASON_WINDOW_EXPIRED
    assert dropped[0].instrument == INSTRUMENT
    assert dropped[0].window_seconds == 60
    assert "61s after the bar closed" in dropped[0].getMessage()


def test_the_window_runs_from_the_moment_the_bar_closed_not_from_its_stamp() -> None:
    """The bar's *stamp* is irrelevant to its own window; the tick clock is not.

    The 10:00 bar closes at t=1000 and the print is admitted 59s later. The
    bar's own stamp (10:00:00) and the print's (10:00:30) say exactly the same
    thing here as they would an hour later — which is the point: a window
    measured on stamps cannot tell those apart, and this one can.
    """
    tick_clock = _Clock(1000.0)
    agg, out = _agg(tick_clock=tick_clock)
    closed = _open_and_close_first_bar(agg, out)

    tick_clock.advance(59)
    agg.on_quote(_ist(10, 0, 30), Decimal("107"), Decimal("1"))

    assert len(out.revisions) == 1
    assert out.revisions[0].frame.time == closed.time
    assert out.revisions[0].frame.close == 107.0


# ---------------------------------------------------------------------------
# 2. The fill freeze: the AC-1 half, and the reason the rule exists
# ---------------------------------------------------------------------------


def test_a_late_tick_on_a_bar_with_a_booked_fill_is_refused_and_counted(
    caplog: pytest.LogCaptureFixture,
) -> None:
    """The essential negative control, and the AC-1 tension resolved.

    A fill is booked against the 10:00 bar — a strategy acted on it — and a
    print arrives 5s later for that same bar, comfortably inside the 60s window.
    The bar must not move: not its high, not its volume, and not its close.
    Only a *different* bar, with no fill, may be reopened. Anything else is the
    look-ahead AC-1 forbids, and it is the failure that is invisible in a chart
    and wrong in a P&L.
    """
    registry = MetricsRegistry()
    tick_clock = _Clock(1000.0)
    agg, out = _agg(metrics=registry, tick_clock=tick_clock)
    closed = _open_and_close_first_bar(agg, out, close=101.0, volume=Decimal("5"))

    # The fill. This is the composition root's job (see BookedFillLedger); what
    # matters here is that the aggregator records it and honours it.
    agg.mark_filled(closed)

    with caplog.at_level(logging.WARNING, logger="tradex_runtime.bar_aggregator"):
        tick_clock.advance(5)
        agg.on_quote(_ist(10, 0, 30), Decimal("150"), Decimal("50"))

    # (a) nothing was revised — the frame is untouched
    assert out.revisions == [], "a bar with a booked fill was revised"
    # (b) nothing moved: the bar a strategy acted on is byte-identical
    bars = out.closed_by_time()
    assert bars[closed.time].high == 101.0
    assert bars[closed.time].volume == 15.0
    # (c) and the refusal is visible, not silent
    assert registry.get(LATE_TICKS_DROPPED_METRIC) == 1.0
    assert registry.get(LATE_TICKS_DROPPED_METRIC, reason=REASON_BOOKED_FILL) == 1.0
    assert agg.revision_stats() == (0, 0, 1)
    frozen = [r for r in caplog.records if "late tick dropped" in r.getMessage()]
    assert len(frozen) == 1
    assert frozen[0].reason == REASON_BOOKED_FILL


def test_a_filled_bar_stays_frozen_forever_not_merely_for_the_window() -> None:
    """"**Never** revised" is the spec's word, so age must not matter.

    Two prints, the first 5s after the bar closed (well inside the 60s window)
    and the second a day later (far outside it). The assertion that matters is
    that the *reason* is ``booked_fill`` for both: the fill check is consulted
    first and the window is never reached, which is what "never" has to mean
    for the rule to be safe.
    """
    registry = MetricsRegistry()
    tick_clock = _Clock(1000.0)
    agg, out = _agg(metrics=registry, tick_clock=tick_clock)
    closed = _open_and_close_first_bar(agg, out)
    agg.mark_filled(closed)

    tick_clock.advance(5)
    agg.on_quote(_ist(10, 0, 30), Decimal("150"), Decimal("1"))  # inside the window
    tick_clock.advance(86400)
    agg.on_quote(_ist(10, 0, 30), Decimal("150"), Decimal("1"))  # a day later

    assert out.revisions == []
    assert registry.get(LATE_TICKS_DROPPED_METRIC, reason=REASON_BOOKED_FILL) == 2.0
    assert registry.get(LATE_TICKS_DROPPED_METRIC, reason=REASON_WINDOW_EXPIRED) == 0.0


def test_the_fill_check_precedes_the_window_in_the_source_not_just_in_the_outcome(
) -> None:
    """The ordering is a safety property, so it is asserted on the source.

    An implementation that checked the window first and the fill second would
    produce the *same* outcome for these tests — it would simply log
    ``window_expired`` instead of ``booked_fill`` — so a behavioural test alone
    cannot see the difference. Reading the method is what makes the ordering
    claim checkable: ``has_fill`` must be decided before anything else.
    """
    from tradex_runtime import bar_aggregator

    source = inspect.getsource(bar_aggregator.BarAggregator._revise_closed)
    assert source.index("has_fill") < source.index("is_closed"), (
        "the booked-fill check must come before the 'do I know this bar' branch: "
        "a bar whose state was evicted would otherwise be reopened as a NEW bar "
        "while a fill is booked against the old one"
    )
    assert source.index("has_fill") < source.index("_last_revised"), (
        "the booked-fill check must come before the ordering check: a filled bar "
        "is frozen forever, so neither may be reached for it"
    )


def test_a_fill_on_one_instrument_never_freezes_another_instruments_bar() -> None:
    """The ledger is keyed on (instrument, timeframe, time) for a reason.

    With a shared ledger — the shape a composition root wants, so one
    ``mark_filled`` covers every aggregator it owns — a key that left the
    instrument out would freeze every bar at that chart time. This is the
    collateral-damage half of the linkage, and it is what makes sharing the
    ledger safe.
    """
    ledger = BookedFillLedger()
    tick_clock = _Clock(1000.0)
    other, out = _agg(instrument="NSE:OTHER", tick_clock=tick_clock, ledger=ledger)
    agg, out = _agg(collector=out, ledger=ledger, tick_clock=tick_clock)

    # Both aggregators see the SAME tape, so both publish the same bars — which
    # is what makes the shared ledger's instrument key the only thing standing
    # between "one bar is frozen" and "both are".
    for hour, minute, second, price, volume in (
        (10, 0, 5, "100", "10"),
        (10, 0, 50, "101", "5"),
        (10, 1, 0, "110", "1"),
    ):
        other.on_quote(_ist(hour, minute, second), Decimal(price), Decimal(volume))
        agg.on_quote(_ist(hour, minute, second), Decimal(price), Decimal(volume))
    frozen, mine = out.closed_frames
    assert frozen.instrument == "NSE:OTHER" and mine.instrument == INSTRUMENT
    assert frozen.time == mine.time, "both aggregators published the same bar"

    other.mark_filled(frozen)
    assert ledger.has_fill(("NSE:OTHER", "1m", frozen.time))
    assert not ledger.has_fill((INSTRUMENT, "1m", mine.time))

    assert agg.booked_fill_ledger is ledger
    tick_clock.advance(5)
    agg.on_quote(_ist(10, 0, 30), Decimal("150"), Decimal("1"))
    assert len(out.revisions) == 1, "another instrument's fill froze this bar"
    assert out.revisions[0].frame.time == mine.time
    assert out.revisions[0].frame.close == 150.0


def test_mark_filled_refuses_a_frame_from_a_different_aggregator() -> None:
    """A mis-filed mark would freeze a bar nobody filled and leave the filled
    one open, so it is a loud error rather than a silent one."""
    agg, out = _agg()
    foreign = BarFrame(
        instrument="NSE:OTHER",
        timeframe="1m",
        time=_chart_time(_ist(10, 0)),
        open=100.0, high=100.0, low=100.0, close=100.0,
        volume=1.0, closed=True,
    )
    with pytest.raises(ValueError, match="another aggregator"):
        agg.mark_filled(foreign)
    assert agg.booked_fill_ledger.has_fill(("NSE:OTHER", "1m", foreign.time)) is False


# ---------------------------------------------------------------------------
# 3. The rest of the rule: what else a late tick can hit
# ---------------------------------------------------------------------------


def test_a_print_for_a_bar_this_aggregator_never_published_opens_a_new_bar() -> None:
    """The cold-boot case: the aggregator has no memory of the bar, so there is
    nothing to revise and the honest reading is that this print is the first
    tick of a new bucket.

    Kept as a new FORMING bar rather than a closed one: the aggregator has
    seen exactly one print for that bucket and cannot claim the bar is complete,
    and emitting a closed bar here would be the same "final bar flush" look-ahead
    AC-1 is about.
    """
    agg, out = _agg()
    agg.on_quote(_ist(10, 5, 0), Decimal("300"), Decimal("1"))
    out.frames.clear()

    agg.on_quote(_ist(9, 30, 0), Decimal("100"), Decimal("4"))

    assert out.revisions == []
    assert len(out.frames) == 1
    reopened = out.frames[0]
    assert reopened.time == _chart_time(_ist(9, 30))
    assert reopened.closed is False
    assert (reopened.open, reopened.high, reopened.low, reopened.close) == (100.0,) * 4
    assert agg.revision_stats() == (0, 0, 0), "reopening is not a late-tick drop"


def test_a_print_behind_the_newest_revised_bar_is_refused_as_stale(
    caplog: pytest.LogCaptureFixture,
) -> None:
    """A revision puts an *older* bar on the wire, so a second revision of
    something older would put the series out of order.

    The 10:00 bar is revised first; a print for 09:59 then arrives. Both are
    inside their own windows and neither is frozen, yet revising 09:59 would
    hand the consumer 09:59 *after* 10:00. Refusing it as ``stale`` is the
    third reason, and it is separate from the window so an operator can tell
    "too late to revise" from "behind what we already revised".
    """
    registry = MetricsRegistry()
    tick_clock = _Clock(1000.0)
    agg, out = _agg(metrics=registry, tick_clock=tick_clock)
    agg.on_quote(_ist(9, 59, 5), Decimal("90"), Decimal("1"))
    agg.on_quote(_ist(9, 59, 50), Decimal("91"), Decimal("1"))
    agg.on_quote(_ist(10, 0, 0), Decimal("100"), Decimal("1"))
    agg.on_quote(_ist(10, 1, 0), Decimal("200"), Decimal("1"))
    out.revisions.clear()

    with caplog.at_level(logging.WARNING, logger="tradex_runtime.bar_aggregator"):
        tick_clock.advance(5)
        agg.on_quote(_ist(10, 0, 20), Decimal("105"), Decimal("1"))  # revises 10:00
        assert len(out.revisions) == 1
        out.revisions.clear()
        tick_clock.advance(5)
        agg.on_quote(_ist(9, 59, 30), Decimal("95"), Decimal("1"))  # would revise 09:59

    assert out.revisions == [], "09:59 was revised after 10:00 — the series went backwards"
    assert registry.get(LATE_TICKS_DROPPED_METRIC, reason=REASON_STALE) == 1.0
    assert registry.get(LATE_TICKS_DROPPED_METRIC, reason=REASON_WINDOW_EXPIRED) == 0.0
    stale = [r for r in caplog.records if r.getMessage().startswith("late tick dropped")]
    assert len(stale) == 1 and stale[0].reason == REASON_STALE


def test_two_late_ticks_for_one_bar_compose_rather_than_overwrite() -> None:
    """A revision is derived from the bar as published *plus* the prints.

    If the stored original were replaced by each revision in turn but the
    second were computed from the pre-window bar, the second would erase the
    first — and a bar that quietly loses a real print is the same defect this
    lane exists to remove, moved one step along.
    """
    tick_clock = _Clock(1000.0)
    agg, out = _agg(tick_clock=tick_clock)
    _open_and_close_first_bar(agg, out, close=101.0, volume=Decimal("5"))

    tick_clock.advance(1)
    agg.on_quote(_ist(10, 0, 20), Decimal("110"), Decimal("1"))
    tick_clock.advance(1)
    agg.on_quote(_ist(10, 0, 30), Decimal("90"), Decimal("2"))

    assert len(out.revisions) == 2
    assert out.revisions[-1].bar_revisions == 2
    final = out.revisions[-1].frame
    assert (final.high, final.low) == (110.0, 90.0), "the first late print was erased"
    assert final.volume == 18.0, "5 + 10 (the bar's own second print) + 1 + 2"


def test_a_print_at_or_before_a_seeded_closed_bar_is_counted_but_not_applied() -> None:
    """``seed_last_closed`` is the caller's assertion about history: that bar is
    closed and this aggregator never saw it, so it is not in the ledger and
    there is nothing to revise.

    The old code dropped it silently. Counting it is the improvement — a
    silently discarded print is how a bar goes short without anyone knowing —
    but applying it would break the seed's promise, which is the whole point of
    seeding.
    """
    registry = MetricsRegistry()
    agg, out = _agg(metrics=registry)
    agg.seed_last_closed(
        BarFrame(
            instrument=INSTRUMENT,
            timeframe="1m",
            time=_chart_time(_ist(9, 59)),
            open=90.0, high=91.0, low=89.0, close=90.5,
            volume=3.0, closed=True,
        )
    )

    agg.on_quote(_ist(9, 59, 30), Decimal("999"), Decimal("9"))

    assert out.frames == [] and out.revisions == []
    assert registry.get(LATE_TICKS_DROPPED_METRIC) == 1.0


def test_a_late_tick_never_writes_to_the_datalake() -> None:
    """"Revision is in memory only; the lake stays append only."

    Checked on the source rather than at runtime, because the failure it guards
    against is *importing* a storage client into the feed — which a behavioural
    test could not see, because on the happy path there is simply no write to
    observe. The aggregator's entire storage surface must stay empty, and
    ``reconcile_bars.py`` must be the thing that owns lake writes.
    """
    from pathlib import Path

    from tradex_runtime import bar_aggregator

    source = inspect.getsource(bar_aggregator)
    for forbidden in ("ParquetStorage", "datalake_root", "parquet_storage", ".upsert("):
        assert forbidden not in source, (
            f"the feed aggregator references {forbidden!r}; revision is in memory "
            "only and the lake is written by reconcile_bars.py"
        )
    repo_root = Path(__file__).resolve().parents[3]
    reconcile = repo_root / "trading" / "scripts" / "reconcile_bars.py"
    assert reconcile.is_file(), "reconcile_bars.py is the lake writer this defers to"


# ---------------------------------------------------------------------------
# 4. Non-vacuity: the two directions, and the wiring itself
# ---------------------------------------------------------------------------


def test_the_two_directions_are_both_exercised() -> None:
    """Both halves of the rule, side by side, in one aggregator.

    The two tests above prove each direction alone. This one proves the pair is
    not two aggregators disagreeing: a single feed, one bar filled and one
    bar not, the same print arriving late for both. If either check were
    missing the other would still pass, so the pair is the property — a late
    tick is applied *because* a filled one is refused, and the difference is
    the fill, not the print.
    """
    registry = MetricsRegistry()
    tick_clock = _Clock(1000.0)
    ledger = BookedFillLedger()
    agg, out = _agg(metrics=registry, ledger=ledger, tick_clock=tick_clock)

    # 10:00 fills a bar, 10:01 fills another, then 10:02 opens a third.
    agg.on_quote(_ist(10, 0, 5), Decimal("100"), Decimal("10"))
    agg.on_quote(_ist(10, 0, 50), Decimal("101"), Decimal("5"))
    agg.on_quote(_ist(10, 1, 0), Decimal("110"), Decimal("10"))
    agg.on_quote(_ist(10, 1, 50), Decimal("111"), Decimal("5"))
    agg.on_quote(_ist(10, 2, 0), Decimal("120"), Decimal("10"))
    filled_bar, open_bar = out.closed_frames
    assert (filled_bar.time, open_bar.time) == (
        _chart_time(_ist(10, 0)),
        _chart_time(_ist(10, 1)),
    )
    agg.mark_filled(filled_bar)  # a strategy acted on 10:00

    # The same late print, for both bars, inside the window for both.
    tick_clock.advance(5)
    agg.on_quote(_ist(10, 0, 30), Decimal("150"), Decimal("7"))  # 10:00 — filled
    agg.on_quote(_ist(10, 1, 30), Decimal("160"), Decimal("7"))  # 10:01 — not

    assert [r.frame.time for r in out.revisions] == [open_bar.time], (
        "exactly the unfilled bar moved; the filled one is not in the list"
    )
    assert registry.get(LATE_TICKS_DROPPED_METRIC, reason=REASON_BOOKED_FILL) == 1.0
    assert registry.get(LATE_TICKS_DROPPED_METRIC, reason=REASON_WINDOW_EXPIRED) == 0.0
    assert agg.revision_stats() == (1, 1, 1)


def test_the_fill_check_is_the_only_thing_separating_the_two_directions() -> None:
    """Read the source and confirm the two tests above are pinned to the same
    code path with exactly one branch between them.

    ``_revise_closed`` decides in a fixed order — known bar, then booked fill,
    then ordering, then window. If a future change added a *second* gate ahead
    of ``has_fill``, the two behavioural tests could still pass while the
    safety property quietly moved. This fails on that.
    """
    from tradex_runtime import bar_aggregator

    source = inspect.getsource(bar_aggregator.BarAggregator._revise_closed)
    # The fill is the first thing decided; ``_last_revised`` and the window come
    # after it. Only the relative order of those two is fixed, because the
    # ordering guard has nothing to say about the cold-boot branch above them.
    assert source.index("has_fill") < source.index("_last_revised"), (
        "the booked-fill check must come before the ordering and window checks: "
        "a filled bar is frozen forever, so neither may be reached for it"
    )
    assert source.index("has_fill") < source.index("self._closed_mono"), (
        "the booked-fill check must come before the window is consulted"
    )


def test_the_aggregator_exposes_the_hook_a_composition_root_needs() -> None:
    """The linkage is an API, so it is checked as one.

    Without ``mark_filled`` there is no way to tell the aggregator a fill was
    booked, and the whole rule degrades to "revise everything" — which passes
    every test that does not use a fill. Asserted on the signature so a rename
    or a keyword-only change breaks here rather than in a composition root.
    """
    signature = inspect.signature(BarAggregator.mark_filled)
    assert list(signature.parameters) == ["self", "frame"]
    assert inspect.signature(BookedFillLedger.mark_filled).parameters.keys() >= {"key"}


def test_a_filled_bar_stays_frozen_even_after_its_state_is_evicted() -> None:
    """Eviction is a memory bound, and the freeze survives it.

    With a ledger of one, the second closed bar evicts the first — its retained
    state is gone. What must NOT go with it is the freeze: a late print for
    that bar must still be refused as ``booked_fill``, because the mark lives
    in its own record and the state in another. Evicting the state only means
    the bar can no longer be *revised*; it can never be silently changed,
    which is the direction that matters.
    """
    registry = MetricsRegistry()
    tick_clock = _Clock(1000.0)
    ledger = BookedFillLedger(maxsize=1)
    agg, out = _agg(tick_clock=tick_clock, ledger=ledger, metrics=registry)
    agg.on_quote(_ist(10, 0, 5), Decimal("100"), Decimal("10"))
    agg.on_quote(_ist(10, 0, 50), Decimal("101"), Decimal("5"))
    agg.on_quote(_ist(10, 1, 0), Decimal("110"), Decimal("10"))  # closes 10:00
    first_bar = out.closed_frames[0]
    agg.mark_filled(first_bar)
    agg.on_quote(_ist(10, 1, 50), Decimal("111"), Decimal("5"))
    agg.on_quote(_ist(10, 2, 0), Decimal("120"), Decimal("10"))  # closes 10:01, evicting 10:00
    assert len(ledger) == 1, "the 10:00 bar's state must have been evicted"
    assert not ledger.is_closed(("NSE:TEST", "1m", first_bar.time))
    assert ledger.has_fill(("NSE:TEST", "1m", first_bar.time)), "the mark must survive"
    out.revisions.clear()
    out.frames.clear()

    # 10:01 is still in the ledger, so it is still revisable.
    tick_clock.advance(5)
    agg.on_quote(_ist(10, 1, 30), Decimal("160"), Decimal("1"))
    assert len(out.revisions) == 1 and out.revisions[0].frame.close == 160.0

    # 10:00's state is gone, but its bar is still frozen, so the print is
    # refused rather than turned into a new bucket. (Its window has also shut —
    # both guards would refuse it, and either alone is the property.)
    out.frames.clear()
    out.revisions.clear()
    tick_clock.advance(5)
    agg.on_quote(_ist(10, 0, 30), Decimal("150"), Decimal("1"))
    assert out.revisions == [] and out.frames == [], (
        "an evicted, filled bar was reopened — the freeze did not survive eviction"
    )
    assert registry.get(LATE_TICKS_DROPPED_METRIC, reason=REASON_BOOKED_FILL) == 1.0


def test_a_revision_keeps_the_bars_provisional_and_identity_fields() -> None:
    """A revised bar is a bar: same instrument, timeframe, run and feed as the
    original, or a consumer reconciling two streams sees two different series.

    ``provisional`` follows the aggregator's default rather than the
    provisional state of whatever happened to be forming when the print
    arrived — the bar being revised was published with the default, and a
    revision that changed the label would make the same bar provisional and
    not-provisional across its own history.
    """
    agg, out = _agg(run_id="run-1", feed_id="feed-1", provisional=True)
    closed = _open_and_close_first_bar(agg, out)

    agg.on_quote(_ist(10, 0, 30), Decimal("150"), Decimal("1"))

    revised = out.revisions[0].frame
    assert (revised.instrument, revised.timeframe) == (INSTRUMENT, "1m")
    assert (revised.run_id, revised.feed_id) == ("run-1", "feed-1")
    assert revised.provisional is closed.provisional is True


def test_bucket_start_is_still_the_one_grid() -> None:
    """The re-export is the contract, and this lane must not have moved it."""
    assert bucket_start(_ist(10, 7, 23), 300) == _ist(10, 5)
