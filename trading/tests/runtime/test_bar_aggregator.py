"""BarAggregator tests — bucket boundaries, throttling, session flush."""

from __future__ import annotations

from datetime import datetime, timedelta
from decimal import Decimal

import pytest
from tradex_domain.enums import Timeframe
from tradex_domain.market_calendar import IST
from tradex_runtime.bar_aggregator import (
    BarAggregator,
    BarFrame,
    _tf_seconds,
    bucket_start,
)


class _Clock:
    """Manual monotonic clock for deterministic throttle behavior."""

    def __init__(self) -> None:
        self.t = 0.0

    def __call__(self) -> float:
        return self.t

    def advance(self, s: float) -> None:
        self.t += s


class _Collector:
    def __init__(self) -> None:
        self.frames: list[BarFrame] = []

    def __call__(self, frame: BarFrame) -> None:
        self.frames.append(frame)

    @property
    def closed_frames(self):
        return [f for f in self.frames if f.closed]

    @property
    def forming_frames(self):
        return [f for f in self.frames if not f.closed]


def _ist(y: int, mo: int, d: int, h: int, mi: int, s: int = 0) -> datetime:
    return datetime(y, mo, d, h, mi, s)


def _agg(timeframe: Timeframe = Timeframe.M1, collector: _Collector | None = None,
         clock: _Clock | None = None) -> BarAggregator:
    return BarAggregator(
        "NSE:TEST",
        timeframe,
        on_frame=collector or _Collector(),
        now=clock or _Clock(),
    )


class TestBucketBoundaries:
    def test_quote_at_bucket_start_opens(self):
        c = _Collector()
        agg = _agg(Timeframe.M1, c)
        agg.on_quote(_ist(2026, 7, 15, 10, 0, 0), Decimal("100"), Decimal("10"))
        assert len(c.forming_frames) == 1
        frame = c.forming_frames[0]
        assert frame.open == frame.high == frame.low == frame.close == 100.0
        assert not frame.closed

    def test_boundary_closes_previous_bar(self):
        """A quote at 10:04:59.9 stays in the 10:00 bar; 10:05:00.1 closes it."""
        c = _Collector()
        agg = _agg(Timeframe.M5, c)
        clock = _Clock()

        agg.on_quote(_ist(2026, 7, 15, 10, 4, 59), Decimal("100"), Decimal("5"))
        # 10:04:59 is still bucket 10:00 (M5 grid).
        forming = agg._bucket
        assert forming.start == _ist(2026, 7, 15, 10, 0)

        clock.advance(2)
        agg.on_quote(_ist(2026, 7, 15, 10, 5, 0), Decimal("101"), Decimal("5"))
        # The 10:00 bucket must have closed at open+... exactly on the boundary.
        assert len(c.closed_frames) == 1
        closed = c.closed_frames[0]
        assert closed.open == 100.0 and closed.close == 100.0
        from zoneinfo import ZoneInfo
        ist = ZoneInfo("Asia/Kolkata")
        assert closed.time == int(
            datetime(2026, 7, 15, 10, 0).replace(tzinfo=ist).timestamp()
        )

    def test_high_low_track_extremes(self):
        c = _Collector()
        agg = _agg(Timeframe.M1, c)
        for i, price in enumerate([100, 105, 98, 103]):
            agg.on_quote(_ist(2026, 7, 15, 10, 0, i % 60), Decimal(price), Decimal("1"))
        frame = agg._bucket
        assert frame.high == 105.0 and frame.low == 98.0 and frame.close == 103.0

    def test_volume_accumulates(self):
        c = _Collector()
        agg = _agg(Timeframe.M1, c)
        for v in (10, 20, 30):
            agg.on_quote(_ist(2026, 7, 15, 10, 0, 30), Decimal("100"), Decimal(v))
        assert agg._bucket.volume == 60.0

    def test_flush_closes_open_bar(self):
        c = _Collector()
        agg = _agg(Timeframe.M1, c)
        agg.on_quote(_ist(2026, 7, 15, 15, 29, 50), Decimal("200"), Decimal("7"))
        assert not c.closed_frames
        agg.flush()
        assert len(c.closed_frames) == 1
        assert c.closed_frames[0].close == 200.0
        agg.flush()  # idempotent
        assert len(c.closed_frames) == 1

    def test_gap_then_new_bucket_starts_fresh(self):
        c = _Collector()
        agg = _agg(Timeframe.M1, c)
        agg.on_quote(_ist(2026, 7, 15, 10, 0, 0), Decimal("100"), Decimal("1"))
        agg.on_quote(_ist(2026, 7, 15, 10, 3, 0), Decimal("110"), Decimal("1"))
        # The 10:00 bar closes with only its own data; the new bucket opens at 110.
        closed = c.closed_frames[0]
        assert closed.close == 100.0 and closed.volume == 1.0
        assert agg._bucket.open == 110.0


class TestThrottling:
    def test_forming_frames_throttled_to_one_per_second(self):
        c = _Collector()
        clock = _Clock()
        agg = _agg(Timeframe.M1, c, clock)
        # 600 quotes inside one bucket, clock advancing 100ms each.
        for i in range(600):
            agg.on_quote(_ist(2026, 7, 15, 10, 0, 0), Decimal("100" + str(i % 10)), Decimal("1"))
            clock.advance(0.1)
        assert len(c.forming_frames) <= 61  # ~1/s over a 60s span

    def test_closed_frame_always_emitted_despite_throttle(self):
        c = _Collector()
        clock = _Clock()
        agg = _agg(Timeframe.M1, c, clock)
        agg.on_quote(_ist(2026, 7, 15, 10, 0, 0), Decimal("100"), Decimal("1"))
        n_before = len(c.frames)
        # Next quote lands past the bucket edge but within the throttle window.
        agg.on_quote(_ist(2026, 7, 15, 10, 1, 0), Decimal("101"), Decimal("1"))
        assert len(c.closed_frames) == 1
        assert len(c.frames) >= n_before + 1


class TestSourceParity:
    def test_identical_quotes_via_live_or_sim_path_produce_same_buckets(self):
        """The aggregator has one code path; this pins the contract both sources get.

        Live and synthetic quotes differ only in arrival timestamps; the same
        sequence of (ts, price, volume) through two fresh aggregators must be
        byte-identical.
        """
        def run(collector):
            agg = BarAggregator("NSE:TEST", Timeframe.M1, on_frame=collector, now=_Clock())
            ts = _ist(2026, 7, 15, 10, 0, 0)
            for i in range(120):  # spans two buckets
                agg.on_quote(ts, Decimal(100 + i % 5), Decimal("2"))
                ts += timedelta(seconds=1)
            agg.flush()

        c1, c2 = _Collector(), _Collector()
        run(c1)
        run(c2)
        assert [
            (f.time, f.open, f.high, f.low, f.close, f.volume, f.closed)
            for f in c1.frames
        ] == [
            (f.time, f.open, f.high, f.low, f.close, f.volume, f.closed)
            for f in c2.frames
        ]
        # And the shape itself: two closed bars + final flush of partial third.
        assert len(c1.closed_frames) == 2


class TestSeededBuckets:
    @staticmethod
    def _frame(
        minute: int,
        *,
        hour: int = 10,
        open_: float = 100.0,
        high: float = 105.0,
        low: float = 99.0,
        close: float = 102.0,
        volume: float = 10.0,
    ) -> BarFrame:
        from zoneinfo import ZoneInfo

        timestamp = datetime(2026, 7, 15, hour, minute).replace(
            tzinfo=ZoneInfo("Asia/Kolkata")
        )
        return BarFrame(
            instrument="NSE:TEST",
            timeframe="1m",
            time=int(timestamp.timestamp()),
            open=open_,
            high=high,
            low=low,
            close=close,
            volume=volume,
            closed=False,
        )

    def test_current_bucket_seed_continues_and_closes_on_boundary(self):
        c = _Collector()
        clock = _Clock()
        agg = _agg(Timeframe.M1, c, clock)
        agg.seed_last_closed(self._frame(59, hour=9))
        agg.seed_current_bucket(self._frame(0))

        agg.on_quote(_ist(2026, 7, 15, 10, 0, 30), Decimal("106"), Decimal("2"))
        forming = c.forming_frames[-1]
        assert forming.time == self._frame(0).time
        assert (forming.open, forming.high, forming.low, forming.close, forming.volume) == (
            100.0,
            106.0,
            99.0,
            106.0,
            12.0,
        )
        assert forming.closed is False

        clock.advance(2)
        agg.on_quote(_ist(2026, 7, 15, 10, 1, 5), Decimal("103"), Decimal("1"))
        closed = c.closed_frames[0]
        assert closed.time == self._frame(0).time
        assert (closed.open, closed.high, closed.low, closed.close, closed.volume) == (
            100.0,
            106.0,
            99.0,
            106.0,
            12.0,
        )
        assert c.forming_frames[-1].time == self._frame(1).time

    def test_last_closed_seed_does_not_reopen_closed_bucket(self):
        c = _Collector()
        agg = _agg(Timeframe.M1, c)
        agg.seed_last_closed(self._frame(0))

        agg.on_quote(_ist(2026, 7, 15, 10, 0, 30), Decimal("999"), Decimal("9"))
        assert c.frames == []

        agg.on_quote(_ist(2026, 7, 15, 10, 1, 5), Decimal("103"), Decimal("1"))
        assert len(c.frames) == 1
        assert c.frames[0].time == self._frame(1).time
        assert c.frames[0].open == 103.0

    def test_provisional_bucket_is_carried_without_closing_early(self):
        c = _Collector()
        clock = _Clock()
        agg = BarAggregator(
            "NSE:TEST",
            Timeframe.M1,
            on_frame=c,
            now=clock,
            provisional=True,
        )
        agg.on_quote(_ist(2026, 7, 15, 10, 0, 5), Decimal("100"), Decimal("1"))
        assert c.frames[0].provisional is True
        assert c.frames[0].closed is False

        clock.advance(1)
        agg.on_quote(_ist(2026, 7, 15, 10, 1, 0), Decimal("101"), Decimal("1"))
        assert c.closed_frames[0].provisional is True
        assert c.closed_frames[0].closed is True
        assert c.forming_frames[-1].provisional is False

    def test_replay_aggregator_does_not_mutate_seeded_live_bucket(self):
        live_collector = _Collector()
        replay_collector = _Collector()
        live = _agg(Timeframe.M1, live_collector)
        live.seed_current_bucket(self._frame(0))
        replay = BarAggregator(
            "NSE:TEST",
            Timeframe.M1,
            on_frame=replay_collector,
            now=_Clock(),
            run_id="run-1",
        )

        replay.on_quote(
            _ist(2026, 7, 15, 10, 0, 15),
            Decimal("200"),
            Decimal("9"),
            source="sim",
        )
        assert live_collector.frames == []
        assert replay_collector.frames[0].source == "sim"
        assert replay_collector.frames[0].run_id == "run-1"

        live.on_quote(_ist(2026, 7, 15, 10, 0, 30), Decimal("106"), Decimal("2"))
        live_frame = live_collector.frames[-1]
        assert live_frame.source == "live"
        assert live_frame.run_id is None
        assert live_frame.open == 100.0
        assert live_frame.close == 106.0


class TestBucketStart:
    def test_floor_onto_grid(self):
        assert bucket_start(_ist(2026, 7, 15, 10, 7, 23), 300) == _ist(2026, 7, 15, 10, 5)
        assert bucket_start(_ist(2026, 7, 15, 10, 0, 0), 300) == _ist(2026, 7, 15, 10, 0)

    def test_daily_floor_uses_ist_midnight(self):
        assert bucket_start(_ist(2026, 7, 15, 23, 59, 59), 86_400) == _ist(2026, 7, 15, 0, 0)

    def test_weekly_floor_uses_ist_monday_midnight(self):
        # W1 is a supported timeframe: the canonical registry maps it to
        # 604800s, so `_tf_seconds` resolves it instead of raising. The floor
        # must be the IST Monday, NOT a 604800s epoch multiple — the epoch
        # origin is a Thursday, so an epoch floor would land mid-week.
        assert _tf_seconds(Timeframe.W1) == 604_800
        # Wednesday and the Sunday that closes the same ISO week both floor
        # to Monday 2026-07-13 00:00 IST.
        assert bucket_start(_ist(2026, 7, 15, 10, 7, 23), 604_800) == _ist(2026, 7, 13, 0, 0)
        assert bucket_start(_ist(2026, 7, 19, 23, 59, 59), 604_800) == _ist(2026, 7, 13, 0, 0)
        # A Monday boundary is its own bucket start (no off-by-one-week).
        assert bucket_start(_ist(2026, 7, 13, 0, 0, 0), 604_800) == _ist(2026, 7, 13, 0, 0)

    def test_unsupported_timeframe_raises(self):
        with pytest.raises(ValueError, match="unsupported"):
            _tf_seconds("not-a-timeframe")


def _epoch_floor(ts: datetime, seconds: int) -> datetime:
    """The grid :func:`bucket_start` replaced: a pure epoch floor, read as IST.

    Kept here as the control rather than trusted to a comment. The two grids
    agree only when *both* the 19,800s IST offset and the open's 33,300s from
    IST midnight are whole multiples of the width. 19,800 divides by 60, 300,
    900 and 1800 but not 3600, and 33,300 by 60, 300 and 900 but not 1800 or
    3600 — which is why 30m was moved by the open alone and hourly by both.
    """
    epoch = int(ts.replace(tzinfo=IST).timestamp())
    return datetime.fromtimestamp(epoch - (epoch % seconds), tz=IST).replace(tzinfo=None)


def _chart_time(ts: datetime) -> int:
    """Chart convention: UTC epoch seconds of an IST wall clock."""
    from zoneinfo import ZoneInfo

    return int(ts.replace(tzinfo=ZoneInfo("Asia/Kolkata")).timestamp())


class TestSessionOpenAnchor:
    """Intraday buckets anchor at the 09:15 session open, not at the epoch.

    33,300s from IST midnight is a whole number of 60, 300 and 900 second bars
    and not of 1800 or 3600, so 30m and hourly were mis-bucketed while
    1m/5m/15m came out right either way — which is why this was invisible on a
    1m chart and obvious on an hourly one.
    """

    #: A plain weekday session, 09:15 to 15:30, as `datetime` stamps.
    OPENING = _ist(2026, 7, 15, 9, 15)
    CLOSE = _ist(2026, 7, 15, 15, 30)

    def test_the_hourly_boundary_falls_where_the_session_grid_says(self):
        """10:14:59 and 10:15:00 are different hourly bars, and the seam is 10:15.

        Under the epoch floor those two ticks were not merely mislabelled, they
        landed in the *same* bar — one stamped 09:30 spanning 09:30-10:30, so
        fifteen minutes of the session's first hour and fifteen of its second.
        Asserting the old grid's collapse too is what makes this a control on
        the fix rather than a restatement of the new answer: an implementation
        that merely picked a different skew would separate the ticks at 10:30
        and pass a boundary test that only looked at 10:15.
        """
        boundary = self.OPENING + timedelta(hours=1)
        assert bucket_start(boundary - timedelta(seconds=1), 3600) == self.OPENING
        assert bucket_start(boundary, 3600) == boundary
        # The old grid: one bar for both, and its own seam half an hour later.
        assert _epoch_floor(boundary - timedelta(seconds=1), 3600) == _ist(
            2026, 7, 15, 9, 30
        )
        assert _epoch_floor(boundary, 3600) == _ist(2026, 7, 15, 9, 30)
        assert _epoch_floor(boundary + timedelta(minutes=14, seconds=59), 3600) == _ist(
            2026, 7, 15, 9, 30
        )
        assert _epoch_floor(boundary + timedelta(minutes=15), 3600) == _ist(
            2026, 7, 15, 10, 30
        )

    def test_one_five_and_fifteen_minutes_are_unmoved(self):
        """The widths the epoch floor already got right stay exactly as they were.

        Bar for bar across a whole session, not at a hand-picked stamp: 19,800
        and 33,300 are both whole multiples of 60, 300 and 900, so the anchored
        grid has to reproduce the epoch floor at every one of those 376 stamps.
        """
        stamps = [self.OPENING + timedelta(minutes=m) for m in range(376)]
        for seconds in (60, 300, 900):
            assert [bucket_start(t, seconds) for t in stamps] == [
                _epoch_floor(t, seconds) for t in stamps
            ], f"the {seconds}s grid must not have moved"

    def test_thirty_minute_and_hourly_bars_cover_the_session_and_no_more(self):
        """The point of anchoring: bars tile the session, ending in a partial.

        6h15m of session is 13 half-hour bars and 7 hourly bars, and at both
        widths the last is a fifteen-minute stub ending at the close. Under the
        epoch floor the 30m grid started at 09:00 — a bar holding only the last
        fifteen minutes before the open — and the hourly grid's first bar was
        08:30 holding the first fifteen, so both series a strategy subscribes to
        were shifted by a quarter of an hour.
        """
        for seconds, count in ((1800, 13), (3600, 7)):
            starts: list[datetime] = []
            t = self.OPENING
            while t < self.CLOSE:
                starts.append(t)
                t += timedelta(seconds=seconds)
            assert starts == [bucket_start(s, seconds) for s in starts]
            assert len(starts) == count
            assert starts[-1] == _ist(2026, 7, 15, 15, 15)
            # The close itself lands in that final stub, never past it.
            assert bucket_start(self.CLOSE, seconds) == starts[-1]
            # And the first bar is the open itself, not a bar reaching back to it.
            assert _epoch_floor(self.OPENING, seconds) != self.OPENING

    def test_a_pre_open_tick_is_not_bucketed_under_an_hour_that_straddles_it(self):
        """The grid runs backwards from the open, monotonically, without relabelling.

        `BarAggregator` decides "close the bar / open a new one" by comparing
        bucket starts, so the floor has to be non-decreasing in the timestamp.
        Floor division gives that for free; a clamp to the open would pass
        monotonicity while filing every pre-open tick into one bar stamped 09:15.
        """
        stamps = [self.OPENING - timedelta(minutes=m) for m in range(120, -1, -1)]
        starts = [bucket_start(t, 3600) for t in stamps]
        assert starts == sorted(starts)
        an_hour_early = self.OPENING - timedelta(hours=1)
        assert bucket_start(an_hour_early, 3600) == an_hour_early
        # The epoch floor put that hour inside a 07:30 bucket that straddles it.
        assert _epoch_floor(an_hour_early, 3600) == _ist(2026, 7, 15, 7, 30)

    def test_the_aggregator_stamps_emitted_hourly_bars_from_the_session_open(self):
        """End to end through the money path, not just the helper.

        A bar that only ever reaches a backtest through ``BarFrame.time`` is
        where the shifted label was observable, so the emitted chart time is
        what this pins.
        """
        c = _Collector()
        agg = _agg(Timeframe.H1, c)
        last_of_first = self.OPENING + timedelta(minutes=59, seconds=59)
        agg.on_quote(self.OPENING, Decimal("100"), Decimal("1"))
        agg.on_quote(last_of_first, Decimal("110"), Decimal("1"))
        agg.on_quote(self.OPENING + timedelta(hours=1), Decimal("120"), Decimal("1"))
        assert [f.time for f in c.closed_frames] == [_chart_time(self.OPENING)]
