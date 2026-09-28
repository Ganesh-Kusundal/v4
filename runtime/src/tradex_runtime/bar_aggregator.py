"""BarAggregator — builds interval bars from a quote stream.

One aggregation code path for every source: live broker quotes (MarketFeed)
and synthetic generator quotes (replay) both feed the same buckets, so a
forming bar means the same thing regardless of where the ticks came from.

The IST session grid rules live in ``tradex_domain.timeframe`` — the one
definition, anchored at the 09:15 session open — because two history sites
bucket the same M1 bars into wider ones and a second copy here is a second
grid. :func:`bucket_start` is re-exported from there, not redefined, so
``domain <- brokers <- trading`` holds and the live feed cannot disagree with
the chart.

The session close is not the aggregator's to decide — :meth:`BarAggregator.flush`
closes whatever is open and it is the caller's to call at 15:30 (or on teardown
or replay stop), so nothing here reads a wall clock and no test has to inject
one to reach the close deterministically.

Late ticks (AC-11, lane 3c)
---------------------------
A quote whose bucket has already been closed — because a later bucket arrived,
or because the session flushed — was dropped outright, so one late print could
silently leave a bar permanently short of a real trade. Such a tick now gets a
**60 second revision window** (:data:`LATE_TICK_REVISION_SECONDS`): a bar stays
revisable for 60 seconds after the moment it was closed, measured on the
aggregator's own monotonic clock (the same ``now`` the forming-frame throttle
already uses), and a print arriving after that is refused.

The clock choice is the whole design, and the stamp-based alternative is
*silently vacuous*. A print's own timestamp and the bar it buckets into come
from the same number, so ``ts - bucket_start(ts)`` is bounded by the bar width
by construction — on a 1m grid no print can be more than 59s behind its own
bar, and a 60s window measured that way could never refuse anything. Arrival
time is not bounded by anything: a broker flushing a backlog sends prints whose
stamps are seconds old but which land minutes late, and those are exactly the
prints a bar can go short on. So the window is a statement about *when the bar
was closed*, not about what any tick claims about itself, and it is the same
kind of statement as :data:`FORMING_FRAME_MIN_INTERVAL` beside it.

Revising a bar means *re-deciding* it, so it is gated on
:meth:`mark_filled`, the aggregator's own record of which bars a fill has been
booked against (see :class:`BookedFillLedger` and the note on that class for
the other half of the contract). A bar with a booked fill is immutable forever,
window or not: a strategy already acted on the original, and moving the bar
under it is exactly the AC-1 look-ahead this lane exists to prevent. The fill
check is the *first* thing :meth:`_revise_closed` decides, ahead of the
cold-boot branch, so no path out of that method can open a bar a fill is booked
against — not the window, and not "this aggregator never saw that bar". The bar
is also not revised by a print for a bar older than the last one already
revised, because that would put an older bar on the wire after a newer one and
every consumer reads these in order.

Every refusal is counted in ``feed_late_ticks_dropped_total`` and logged with
its reason, labelled ``reason=`` so an operator can tell "a fill froze this bar"
from "this print was hours late" without reading logs.

**Revision is in memory only.** The revision is a corrected frame on the
aggregator's ``on_frame`` callback, like any other frame; nothing here writes to
the datalake. The lake stays append-only, and ``trading/scripts/reconcile_bars.py``
is the tool that owns writing a corrected bar over a stored one.
"""

from __future__ import annotations

import logging
import math
import time
from collections import OrderedDict
from collections.abc import Callable
from dataclasses import dataclass
from datetime import datetime
from decimal import Decimal
from typing import Any

from tradex_domain.enums import Timeframe
from tradex_domain.timeframe import bucket_seconds, bucket_start

log = logging.getLogger(__name__)

#: Minimum spacing between two forming-bar frames for one (bucket) — a burst
#: of quotes must not flood the socket. Closed-bar frames bypass the throttle.
FORMING_FRAME_MIN_INTERVAL = 1.0

#: How long a closed bar stays revisable after the moment it was closed
#: (AC-11). Sixty seconds of *arrival* time, not of tick age — see the module
#: docstring for why the stamp-based reading cannot work. It is a flat number
#: rather than a multiple of the bar width because a wider bar is not "more
#: revisable": it closed later, so its own 60 seconds start later.
LATE_TICK_REVISION_SECONDS = 60

#: Counter for late ticks that were NOT applied to a bar. One family, indexed
#: by ``reason``: ``booked_fill`` (AC-1 says the bar is frozen forever),
#: ``window_expired`` (the print was behind the window), ``stale`` (it belongs
#: to a bar older than one already revised). Exported as a name constant so the
#: alert rule, the metrics catalog and this module cannot drift apart.
LATE_TICKS_DROPPED_METRIC = "feed_late_ticks_dropped_total"

#: :class:`BookedFillLedger` size. One trading day of M1 bars is ~375, so 8192
#: holds a whole session of M1 (or a whole day of 5m with room to spare) while
#: bounding the memory an unfilled tape can cost.
DEFAULT_FILLED_LEDGER_SIZE = 8192

#: Why a late tick did not reach a bar. Also the ``reason`` label values.
REASON_BOOKED_FILL = "booked_fill"
REASON_WINDOW_EXPIRED = "window_expired"
REASON_STALE = "stale"


@dataclass(frozen=True, slots=True)
class BarFrame:
    """One emitted bar: forming (closed=False) or final (closed=True)."""

    instrument: str  # instrument_id string, e.g. 'NSE:RELIANCE'
    timeframe: str  # canonical Timeframe value, e.g. '1m'
    time: int  # UTC epoch seconds of bucket start (chart convention)
    open: float
    high: float
    low: float
    close: float
    volume: float
    closed: bool
    source: str = "live"  # 'live' broker quotes vs 'sim' replay ticks
    run_id: str | None = None
    provisional: bool = False
    feed_id: str | None = None
    connection_generation: int | None = None
    quality_flags: tuple[str, ...] = ()


def _tf_seconds(timeframe: Timeframe) -> int:
    """Bucket width in seconds for *timeframe*, from the canonical registry."""
    try:
        return bucket_seconds(timeframe)
    except KeyError:
        raise ValueError(f"bar aggregation unsupported for timeframe {timeframe}") from None

class _Bucket:
    """One accumulating bar."""

    __slots__ = ("start", "open", "high", "low", "close", "volume")

    def __init__(self, start: datetime, o: float, h: float, low: float, c: float, v: float) -> None:
        self.start = start
        self.open = o
        self.high = h
        self.low = low
        self.close = c
        self.volume = v

    def absorb(self, price: float, volume: float) -> None:
        self.high = max(self.high, price)
        self.low = min(self.low, price)
        self.close = price
        self.volume += volume

    def copy(self) -> _Bucket:
        """An independent snapshot of this bar.

        The ledger keeps one of these rather than the aggregator's own bucket:
        the aggregator drops its bucket on the next boundary and keeps mutating
        whichever bucket is forming, and a revision computed from a moving
        target is not a revision of anything.
        """
        return _Bucket(self.start, self.open, self.high, self.low, self.close, self.volume)


def _apply_late_tick(bucket: _Bucket, price: float, volume: float) -> _Bucket:
    """A copy of *bucket* with one more print absorbed into it.

    The print lands where any other would have: high/low widen, close becomes
    this print's price, volume adds. It does not touch ``open`` — the first
    print in the bar still sets it, and a print that arrived late is not the
    first one.
    """
    revised = bucket.copy()
    revised.absorb(price, volume)
    return revised


@dataclass(frozen=True, slots=True)
class RevisedBar:
    """One closed bar as it stands after a late print landed on it.

    Emitted through the same ``on_frame`` callback as every other frame, so a
    consumer that already tracks bars needs no new subscription and no new
    transport field: the ``closed`` flag says how to read it. What a consumer
    *does* need to know is that this frame's ``time`` may be **older** than the
    last frame it was handed, so it must key bars by ``time`` rather than
    append. The other fields say what happened, when the bar's window shuts,
    and how many late prints have landed on it so far.
    """

    frame: BarFrame
    reason: str
    window_closes_at: float
    bar_revisions: int


class BookedFillLedger:
    """Which bars this aggregator has published, and which of them are frozen.

    Two questions need two records, and conflating them is the trap:

    * **Has this bar been published at all?** ``_closed_at`` remembers every
      closed bar, because a late print is only revisable if the bar exists —
      an aggregator with no state has nothing to revise, and pretending
      otherwise would let a print from three hours ago open a new bar.
    * **Has a fill been booked against it?** ``_filled`` remembers the bars a
      strategy acted on, and it is consulted first, ahead of the window and
      ahead of ``_closed_at``: a bar with a booked fill is immutable *forever*,
      so no window and no age can reopen it.

    The fill side is the AC-1 tension this class resolves. There is no
    *reliable* bar-to-fill key in the platform to import — the closest is
    ``OrderRequest.reference_timestamp``, which holds the *event* timestamp the
    strategy saw, not a bar identity, and which is a datetime while bars are
    identified by ``BarFrame.time`` (UTC epoch seconds of the IST bucket
    start). Guessing a join between the two would silently freeze the wrong bar
    or the wrong day and reopen a bar a fill was booked against, which is a
    worse failure than not revising at all. So the link is **established
    deliberately** by the composition root calling :meth:`mark_filled` with the
    chart time it read off the emitted frame, and a bar nothing called that for
    is a bar nothing filled against.

    That is the safe direction to fail: "unmarked" is read as "no fill", which
    is exactly the reading the pre-3c code had and which AC-1 already governs
    elsewhere. A *missing* mark is therefore never silently dangerous here, but
    a *wrong* one would be — so :meth:`BarAggregator.mark_filled` takes a
    ``BarFrame`` (whose ``time`` is a chart epoch that cannot be confused with a
    quote timestamp) rather than a free-form integer, and the ledger keys on
    ``(instrument, timeframe, time)`` so a mark for one instrument can never
    freeze another's bar.

    **The freeze outlives the state it protects.** ``_filled`` is a separate
    record from ``_closed_at``, and it is the one consulted first, so evicting a
    bar's retained state cannot un-freeze it: the bar becomes un-revisable and
    un-reopenable, which is the safe direction. That is not an accident of the
    ordering — it is why the fill check runs before the "do I know this bar at
    all" branch in :meth:`BarAggregator._revise_closed`.

    Bounded by ``maxsize``: the oldest bar's retained state is dropped, and its
    *mark* is a separate entry that is only dropped when the mark table is
    itself full. A trade day is ~375 M1 bars and 8192 holds several days, so the
    ledger is bounded by construction. Not thread-safe by itself: the aggregator
    it belongs to is not, either.
    """

    def __init__(self, maxsize: int = DEFAULT_FILLED_LEDGER_SIZE) -> None:
        self._maxsize = max(1, int(maxsize))
        self._closed_at: OrderedDict[tuple[str, str, int], _Bucket] = OrderedDict()
        self._filled: OrderedDict[tuple[str, str, int], None] = OrderedDict()
        self._revisions: dict[tuple[str, str, int], int] = {}

    def _trim(self, store: OrderedDict[Any, Any], maxsize: int) -> None:
        while len(store) > maxsize:
            store.popitem(last=False)

    def record_closed(self, key: tuple[str, str, int], bar: _Bucket) -> None:
        """Remember that the bar at *key* was published closed, and keep its state.

        *bar* is the bucket as emitted — a snapshot is taken, because the
        aggregator's own bucket is about to be replaced by the next one.
        """
        self._closed_at[key] = bar.copy()
        self._closed_at.move_to_end(key)
        self._trim(self._closed_at, self._maxsize)

    def is_closed(self, key: tuple[str, str, int]) -> bool:
        """Whether the bar at *key* has already been published closed."""
        return key in self._closed_at

    def original(self, key: tuple[str, str, int]) -> _Bucket | None:
        """The retained state of the closed bar at *key*, or ``None``."""
        return self._closed_at.get(key)

    def set_original(self, key: tuple[str, str, int], bar: _Bucket) -> None:
        """Replace the retained state of *key* after a successful revision."""
        self._closed_at[key] = bar
        self._closed_at.move_to_end(key)
        self._revisions[key] = self._revisions.get(key, 0) + 1

    def revision_count(self, key: tuple[str, str, int]) -> int:
        """How many late prints have already revised the bar at *key*."""
        return self._revisions.get(key, 0)

    def mark_filled(self, key: tuple[str, str, int]) -> None:
        """Freeze the bar at *key* forever: a fill has been booked against it."""
        self._filled[key] = None
        self._filled.move_to_end(key)
        self._trim(self._filled, self._maxsize)

    def has_fill(self, key: tuple[str, str, int]) -> bool:
        """Whether a fill is booked against the bar at *key*."""
        return key in self._filled

    def clear(self) -> None:
        """Forget everything (session restart / generation reset)."""
        self._closed_at.clear()
        self._filled.clear()
        self._revisions.clear()

    def __len__(self) -> int:
        return len(self._closed_at)


class BarAggregator:
    """Accumulates quotes into interval bars for ONE (instrument, timeframe).

    Not thread-safe by itself: callers on broker receive threads serialize
    through their own lock or the ThreadSafeReactiveBus before calling.

    A late tick for a bucket that is already closed no longer vanishes: it goes
    through :meth:`on_quote` -> :meth:`_revise_closed`, which may re-send the
    corrected bar on the same ``on_frame`` callback, or may count and log the
    refusal in ``feed_late_ticks_dropped_total``. ``metrics`` is the
    duck-typed registry (``MetricsRegistry``) the other feed modules take;
    ``None`` means counting is off and the decision is still made and logged.
    """

    def __init__(
        self,
        instrument_id: str,
        timeframe: Timeframe,
        *,
        on_frame: Callable[[BarFrame], None],
        now: Callable[[], float] = time.monotonic,
        run_id: str | None = None,
        provisional: bool = False,
        feed_id: str | None = None,
        connection_generation: int | None = None,
        quality_flags: tuple[str, ...] = (),
        metrics: Any | None = None,
        ledger: BookedFillLedger | None = None,
        late_tick_window: int = LATE_TICK_REVISION_SECONDS,
    ) -> None:
        self._instrument_id = instrument_id
        self._timeframe = timeframe
        self._seconds = _tf_seconds(timeframe)
        self._on_frame = on_frame
        self._now = now
        self._run_id = run_id
        self._feed_id = feed_id
        self._connection_generation = connection_generation
        self._quality_flags = tuple(quality_flags)
        self._bucket: _Bucket | None = None
        self._source = "live"
        self._disposed = False
        self._provisional_default = bool(provisional)
        self._provisional = bool(provisional)
        self._has_streamed_bucket = False
        self._seeded_closed_start: datetime | None = None
        # Negative infinity so the very first quote emits immediately no
        # matter what clock the caller injected (a test clock at zero would
        # otherwise swallow the first frame).
        self._last_forming_emit = float("-inf")
        self._last_closed: int | None = None
        # -- late-tick revision (AC-11, lane 3c) --------------------------
        self._late_window = float(late_tick_window)
        self._metrics = metrics
        self._ledger = ledger if ledger is not None else BookedFillLedger()
        # Chart time of the newest bar this aggregator has REVISED, or None.
        # A print for anything older would put a bar back in time on the wire.
        self._last_revised: int | None = None
        self._revisions = 0
        self._late_ticks_applied = 0
        self._late_ticks_dropped = 0
        # Monotonic time each closed bar's revision window runs from: chart_time
        # -> when it closed, kept only while the window is still open. Pruned on
        # every close, so it holds one entry per bar that closed inside the
        # window — one or two at M1, one at M5 — and a quiet feed costs nothing.
        self._closed_mono: dict[int, float] = {}
        self._last_close_at = float("-inf")

    # ------------------------------------------------------------------ input

    def seed_last_closed(self, bar: BarFrame) -> None:
        self._validate_seed(bar)
        self._seeded_closed_start = self._seed_start(bar.time)
        self._last_closed = self._chart_time(self._seeded_closed_start)

    def seed_current_bucket(self, bar: BarFrame, *, provisional: bool | None = None) -> None:
        self._validate_seed(bar)
        if bar.closed:
            raise ValueError("current bucket seed must be forming")
        if self._bucket is not None:
            raise ValueError("cannot seed a bucket after aggregation starts")
        start = self._seed_start(bar.time)
        if self._seeded_closed_start is not None and start <= self._seeded_closed_start:
            raise ValueError("current bucket must follow the last closed bucket")
        self._bucket = _Bucket(start, bar.open, bar.high, bar.low, bar.close, bar.volume)
        self._provisional = self._provisional_default if provisional is None else bool(provisional)
        self._has_streamed_bucket = True
        self._last_forming_emit = float("-inf")

    def _validate_seed(self, bar: BarFrame) -> None:
        if bar.instrument != self._instrument_id or bar.timeframe != self._timeframe.value:
            raise ValueError("bar seed does not match aggregator")
        values = (bar.open, bar.high, bar.low, bar.close, bar.volume)
        if any(not math.isfinite(float(value)) for value in values):
            raise ValueError("bar seed values must be finite")

    def _seed_start(self, chart_time: int) -> datetime:
        from zoneinfo import ZoneInfo

        aware = datetime.fromtimestamp(int(chart_time), tz=ZoneInfo("Asia/Kolkata"))
        return bucket_start(aware.replace(tzinfo=None), self._seconds)

    def _chart_time(self, start: datetime) -> int:
        from zoneinfo import ZoneInfo

        return int(start.replace(tzinfo=ZoneInfo("Asia/Kolkata")).timestamp())

    def on_quote(
        self,
        ts_ist: datetime,
        price: Decimal | float,
        volume: Decimal | float | None,
        source: str = "live",
    ) -> None:
        """Feed one trade print (IST-naive timestamp like the datalake).

        ``source`` rides on every frame this quote emits so a consumer can
        tell broker ticks from synthetic replay ticks.

        A print for a bucket that is already closed is a late tick: it is
        offered to :meth:`_revise_closed` rather than dropped, and that method
        decides. Dropping it here is what made a single out-of-order print a
        permanent hole in a bar the strategy then acted on."""
        if self._disposed:
            return
        p = float(price)
        v = float(volume) if volume is not None else 0.0
        start = bucket_start(ts_ist, self._seconds)
        now = self._now()

        starting_new = False
        if self._bucket is not None:
            if start < self._bucket.start:
                self._revise_closed(start, now, p, v, source)
                return
            self._source = source
            if start > self._bucket.start:
                self._emit(closed=True, closed_at=now)
                starting_new = True

        if self._bucket is None:
            if self._seeded_closed_start is not None and start <= self._seeded_closed_start:
                # A bucket that was seeded CLOSED is history the feed promised
                # it would not reopen, and it is not in ``_ledger`` either: the
                # seed is the caller's assertion about the past, not a bar this
                # aggregator published. The old behaviour (drop silently) is
                # right here; a late tick is counted and logged rather than
                # discarded, but it is not applied.
                self._count_late_drop(
                    REASON_WINDOW_EXPIRED,
                    start=start,
                    price=p,
                    volume=v,
                    detail="at or before the seeded last closed bar",
                )
                return
            starting_new = True

        if starting_new:
            self._source = source
            self._provisional = (
                self._provisional_default if not self._has_streamed_bucket else False
            )
            self._bucket = _Bucket(start, p, p, p, p, v)
        else:
            assert self._bucket is not None
            self._bucket.absorb(p, v)
        self._has_streamed_bucket = True

        # Forming-bar updates are throttled for live broker quotes; simulation / replay
        # ticks must emit every intra-candle update so candle formation is visible on the chart.
        if (
            source == "sim"
            or starting_new
            or now - self._last_forming_emit >= FORMING_FRAME_MIN_INTERVAL
        ):
            self._last_forming_emit = now
            self._emit(closed=False)

    # ------------------------------------------------------- late-tick revision

    def mark_filled(self, frame: BarFrame) -> None:
        """Declare that a fill is booked against *frame*'s bar: freeze it.

        The composition root calls this with the frame (or bar record) a fill
        was booked from. From that moment the bar is never revised, whatever
        arrives late for it — the AC-1 half of this lane: a strategy acted on
        the original, so moving the bar is the look-ahead the criterion
        forbids.

        Takes a :class:`BarFrame` rather than a raw epoch so the key can only
        be a chart time read off a bar this aggregator published. A frame from
        another instrument or timeframe raises rather than being filed against
        the wrong bar: a silent no-op here is a fill that went unrecorded, and
        an unrecorded fill is what reopens a frozen bar.
        """
        if frame.instrument != self._instrument_id or frame.timeframe != self._timeframe.value:
            raise ValueError("cannot mark a bar from another aggregator as filled")
        self._ledger.mark_filled((self._instrument_id, self._timeframe.value, int(frame.time)))

    @property
    def booked_fill_ledger(self) -> BookedFillLedger:
        """The published/frozen record, for a caller wiring several aggregators."""
        return self._ledger

    @property
    def late_tick_window(self) -> float:
        """Seconds after a bar is closed within which a print revises it."""
        return self._late_window

    def revision_stats(self) -> tuple[int, int, int]:
        """``(revisions, late_ticks_applied, late_ticks_dropped)`` for this aggregator.

        Cheap to read and independent of whether a registry was wired, so an
        operator (or a test) can see the shape of the late-tick traffic even
        with no metrics sink attached.
        """
        return self._revisions, self._late_ticks_applied, self._late_ticks_dropped

    def _revise_closed(
        self,
        start: datetime,
        now: float,
        price: float,
        volume: float,
        source: str,
    ) -> None:
        """Offer a late print that arrived at monotonic *now* to the bar at *start*.

        Four answers, in the order they are asked, and the order *is* the rule:

        1. **Does the bar carry a booked fill?** Then it is frozen forever. This
           is asked *first* and unconditionally, because it is the one answer
           that may never be wrong: a bar somebody traded must not move at any
           age, and the question does not depend on what this aggregator still
           remembers about the bar. Asking it *after* the cold-boot branch
           below would let a bar whose state was evicted (or that another
           aggregator published) be reopened as a *new* bar while a fill sits
           against the old one — the bar would change identity and the freeze
           would have missed it.
        2. **Is the bar in the ledger at all?** No — this aggregator never
           published it, so there is nothing to revise and the print opens a
           new bucket instead (a cold boot replaying an old tape).
        3. **Is the bar behind the newest bar already revised?** Then re-sending
           it would put an *older* bar on the wire after a newer one, and a
           chart or a consumer that applies frames in order would read a time
           series that goes backwards.
        4. **Did the print arrive inside the window?** Only then is the bar
           rebuilt from the stored original plus this print and re-sent.
        """
        key = (self._instrument_id, self._timeframe.value, self._chart_time(start))
        if self._ledger.has_fill(key):
            self._count_late_drop(
                REASON_BOOKED_FILL,
                start=start,
                price=price,
                volume=volume,
                detail="bar is frozen by a booked fill (AC-1)",
            )
            return

        if not self._ledger.is_closed(key):
            # Answer 2: reopen as a NEW bar rather than revise. This is the
            # cold-boot / replay-after-restart case, where the aggregator has
            # no memory of the bar because it never saw it, and treating the
            # print as the first tick of a new bucket is the honest reading.
            self._source = source
            self._provisional = self._provisional_default
            self._bucket = _Bucket(start, price, price, price, price, volume)
            self._has_streamed_bucket = True
            self._last_forming_emit = float("-inf")
            self._emit(closed=False)
            return

        if self._last_revised is not None and key[2] < self._last_revised:
            self._count_late_drop(
                REASON_STALE,
                start=start,
                price=price,
                volume=volume,
                detail=f"behind the newest revised bar {self._last_revised}",
            )
            return

        # Answer 4: the window. The edge is inclusive — a print arriving exactly
        # 60s after the close is still inside, the same reading
        # FORMING_FRAME_MIN_INTERVAL gives its own interval beside it.
        closed_mono = self._closed_mono.get(key[2])
        if closed_mono is None or now > closed_mono + self._late_window:
            age = f"{now - closed_mono:.0f}s" if closed_mono is not None else "unknown"
            self._count_late_drop(
                REASON_WINDOW_EXPIRED,
                start=start,
                price=price,
                volume=volume,
                detail=f"arrived {age} after the bar closed, outside the "
                       f"{self._late_window:g}s revision window",
            )
            return

        original = self._ledger.original(key)
        if original is None:  # pragma: no cover - is_closed() already proved it
            self._count_late_drop(
                REASON_WINDOW_EXPIRED,
                start=start,
                price=price,
                volume=volume,
                detail="bar state no longer retained",
            )
            return
        revised = _apply_late_tick(original, price, volume)
        # The stored original becomes the new original: a second late print for
        # the same bar must see the first one's effect, or the two would each
        # rebuild from the pre-window bar and the second would erase the first.
        self._ledger.set_original(key, revised)
        self._last_revised = key[2]
        self._revisions += 1
        self._late_ticks_applied += 1
        log.info(
            "bar revised by a late tick: %s %s bar %s close %s -> %s",
            self._instrument_id,
            self._timeframe.value,
            start.isoformat(),
            original.close,
            revised.close,
            extra={
                "stage": "bar.revised",
                "instrument": self._instrument_id,
                "timeframe": self._timeframe.value,
                "bar_time": start.isoformat(),
                "arrived_at": now,
                "open": revised.open,
                "high": revised.high,
                "low": revised.low,
                "close": revised.close,
                "volume": revised.volume,
            },
        )
        self._on_frame(
            RevisedBar(
                frame=BarFrame(
                    instrument=self._instrument_id,
                    timeframe=self._timeframe.value,
                    time=key[2],
                    open=revised.open,
                    high=revised.high,
                    low=revised.low,
                    close=revised.close,
                    volume=revised.volume,
                    closed=True,
                    source=source,
                    run_id=self._run_id,
                    provisional=self._provisional_default,
                    feed_id=self._feed_id,
                    connection_generation=self._connection_generation,
                    quality_flags=self._quality_flags,
                ),
                reason="late_tick",
                window_closes_at=self._closed_mono[key[2]] + self._late_window,
                bar_revisions=self._ledger.revision_count(key),
            )
        )

    def _count_late_drop(
        self,
        reason: str,
        *,
        start: datetime,
        detail: str,
        price: float = 0.0,
        volume: float = 0.0,
    ) -> None:
        """Count and log one late tick that was NOT applied.

        WARNING, not INFO: a dropped late tick is a hole in a bar the tape
        really printed, and on a live feed the only record of it is this line
        and the counter. The count goes to
        ``feed_late_ticks_dropped_total{reason=...}`` — the reason is the
        label, so ``booked_fill`` (a frozen bar, working as designed) is
        separable from ``window_expired`` (the tape is genuinely behind) and
        from ``stale`` (a print for a bar behind one already revised).

        *price*/*volume* default so a caller with no print in hand (the seeded
        history case) still reports the drop; the fields are diagnostic, and a
        missing one must never cost the count and the log line, which are the
        whole point of this method.
        """
        self._late_ticks_dropped += 1
        if self._metrics is not None:
            self._metrics.counter(LATE_TICKS_DROPPED_METRIC).inc(reason=reason)
        log.warning(
            "late tick dropped (%s): %s %s bar %s: %s",
            reason,
            self._instrument_id,
            self._timeframe.value,
            start.isoformat(),
            detail,
            extra={
                "stage": "bar.late_tick_dropped",
                "instrument": self._instrument_id,
                "timeframe": self._timeframe.value,
                "reason": reason,
                "bar_time": start.isoformat(),
                "price": price,
                "volume": volume,
                "window_seconds": self._late_window,
            },
        )

    def flush(self) -> None:
        """Close any open bucket (session end / teardown). Idempotent."""
        if self._bucket is not None and not self._disposed:
            self._emit(closed=True, closed_at=self._now())

    def dispose(self, *, flush: bool = True) -> None:
        if self._disposed:
            return
        try:
            if flush:
                self.flush()
        finally:
            self._disposed = True
            self._bucket = None

    @property
    def last_closed_time(self) -> int | None:
        """UTC seconds of the last closed bucket start, or None."""
        return getattr(self, "_last_closed", None)

    # ----------------------------------------------------------------- output

    def _prune_windows(self, now: float) -> None:
        """Forget every closed bar whose revision window has already shut."""
        self._closed_mono = {
            chart_time: closed
            for chart_time, closed in self._closed_mono.items()
            if now <= closed + self._late_window
        }

    def _emit(self, *, closed: bool, closed_at: float | None = None) -> None:
        assert self._bucket is not None
        b = self._bucket
        chart_time = self._chart_time(b.start)
        if closed:
            # A closed bar is now revisable, so it joins the ledger — with its
            # state, because a revision is computed from the bar as published
            # plus the late prints. Recorded BEFORE the callback on purpose: a
            # subscriber that books a fill synchronously calls ``mark_filled``
            # with the frame it was just handed, and that mark is only honoured
            # if the bar is already findable.
            self._ledger.record_closed(
                (self._instrument_id, self._timeframe.value, chart_time), b
            )
            # ...and it opens its revision window, measured from the moment it
            # closed. A forming ``_emit`` closes nothing and opens nothing.
            when = self._now() if closed_at is None else float(closed_at)
            self._last_close_at = when
            self._closed_mono[chart_time] = when
            self._prune_windows(when)
        self._on_frame(
            BarFrame(
                instrument=self._instrument_id,
                timeframe=self._timeframe.value,
                time=chart_time,
                open=b.open,
                high=b.high,
                low=b.low,
                close=b.close,
                volume=b.volume,
                closed=closed,
                source=self._source,
                run_id=self._run_id,
                provisional=self._provisional,
                feed_id=self._feed_id,
                connection_generation=self._connection_generation,
                quality_flags=self._quality_flags,
            )
        )
        if closed:
            self._last_closed = chart_time
            self._bucket = None
            self._last_forming_emit = self._now()


__all__ = [
    "BarAggregator",
    "BarFrame",
    "BookedFillLedger",
    "DEFAULT_FILLED_LEDGER_SIZE",
    "LATE_TICKS_DROPPED_METRIC",
    "LATE_TICK_REVISION_SECONDS",
    "REASON_BOOKED_FILL",
    "REASON_STALE",
    "REASON_WINDOW_EXPIRED",
    "RevisedBar",
    "bucket_start",
]
