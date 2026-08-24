"""BarAggregator tests — bucket boundaries, throttling, session flush."""

from __future__ import annotations

from datetime import datetime, timedelta
from decimal import Decimal

import pytest

from tradex_domain.enums import Timeframe
from tradex_trading.runtime.bar_aggregator import BarAggregator, bucket_start


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
        self.frames = []

    def __call__(self, frame) -> None:
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
        assert [(f.time, f.open, f.high, f.low, f.close, f.volume, f.closed) for f in c1.frames] == [
            (f.time, f.open, f.high, f.low, f.close, f.volume, f.closed) for f in c2.frames
        ]
        # And the shape itself: two closed bars + final flush of partial third.
        assert len(c1.closed_frames) == 2


class TestBucketStart:
    def test_floor_onto_grid(self):
        assert bucket_start(_ist(2026, 7, 15, 10, 7, 23), 300) == _ist(2026, 7, 15, 10, 5)
        assert bucket_start(_ist(2026, 7, 15, 10, 0, 0), 300) == _ist(2026, 7, 15, 10, 0)

    def test_unsupported_timeframe_raises(self):
        with pytest.raises(ValueError, match="unsupported"):
            _agg(Timeframe.W1)
