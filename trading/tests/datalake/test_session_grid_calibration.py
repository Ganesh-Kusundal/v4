"""The two numbers lane 0d recalibrated, pinned against how they were derived.

Anchoring intraday bars to the session open (09:15) changes the grid the repair
tooling measures against, so ``GapDetector``'s floor and ``datalake_stats``'s
density window were re-derived. Both derivations turn on arithmetic rather than
taste, which is what makes them worth asserting: a number written into a
comment drifts silently, and a drifted floor either manufactures false problems
every run or hides a real hole forever.

1. **The 1m grid is anchor-invariant.** The IST offset is 19,800s, so flooring
   a timestamp to the epoch and anchoring it at 09:15 produce *identical* 1m
   bucket starts. That is why the density window did not move, and why the
   floor could be derived from venue ceilings rather than from the bar grid.
   It is also why the defect this lane fixes was invisible on a 1m chart and
   obvious on an hourly one.

2. **Minutes after 15:14 are the close, not a hole.** Dhan's NSE 1m series
   stops at 15:14, so 15:15..15:29 is 15 stamps the venue will not send.
   ``GapDetector`` classifies them with the session close. A floor is not what
   keeps them out of the fetch.
"""

from __future__ import annotations

import importlib.util
from datetime import date, datetime, timedelta
from datetime import time as dtime
from pathlib import Path

import pandas as pd
from tradex_domain.market_calendar import (
    IST,
    MARKET_CLOSE,
    MARKET_OPEN,
    NSE_HOLIDAYS_2026,
)
from tradex_market_data.gap_detector import (
    GapDetector,
    _edge_stamps,
    _session_grid,
)
from tradex_market_data.parquet_storage import ParquetStorage
from tradex_runtime.bar_aggregator import bucket_start

_REPO_ROOT = Path(__file__).resolve().parents[3]

#: Last 1m stamp Dhan's NSE intraday endpoint serves. Measured, not documented:
#: its NSE 1m series stops at 15:14 whatever window is asked for.
DHAN_LAST_1M_STAMP = dtime(15, 14)

#: A weekday with no NSE holiday, so the session grid is the plain weekday one.
_SESSION_DAY = date(2026, 7, 1)

#: Every intraday width the canonical registry serves.
_INTRADAY_SECONDS = [60, 300, 900, 1800, 3600]


def _ist_offset() -> int:
    """Seconds IST is offset from UTC — the reason the epoch grid is skewed."""
    return int(IST.utcoffset(None).total_seconds())


def _open_seconds() -> int:
    """The session open's seconds from IST midnight."""
    return MARKET_OPEN.hour * 3600 + MARKET_OPEN.minute * 60


def _grid() -> pd.DatetimeIndex:
    """The 1m session grid for one session, exactly as the scan builds it."""
    return _session_grid(
        datetime.combine(_SESSION_DAY, MARKET_OPEN),
        datetime.combine(_SESSION_DAY, MARKET_CLOSE),
        "1min",
        NSE_HOLIDAYS_2026,
    )


def _interior() -> list[pd.Timestamp]:
    """The session's interior stamps — neither session edge."""
    opens, closes = _edge_stamps(_grid())
    return [t for t in _grid() if t not in opens and t not in closes]


def _dhan_tail_stamps() -> int:
    """Interior stamps a Dhan-only session can never hold."""
    return sum(1 for t in _interior() if t.time() > DHAN_LAST_1M_STAMP)


#: A caller-chosen floor. It hides short interior holes; it does not decide
#: the Dhan ceiling. The ceiling is classified before the floor runs.
FLOOR = 16


def _epoch_floor(ts: datetime, seconds: int) -> datetime:
    """The grid this lane replaced: a pure epoch floor, read as IST wall clock."""
    epoch = int(ts.replace(tzinfo=IST).timestamp())
    return datetime.fromtimestamp(epoch - (epoch % seconds), tz=IST).replace(
        tzinfo=None
    )


def _stats_module():
    path = _REPO_ROOT / "trading" / "scripts" / "datalake_stats.py"
    spec = importlib.util.spec_from_file_location("_lane0d_datalake_stats", path)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


class TestOneMinuteGridIsAnchorInvariant:
    """Why the density window did not move: the 1m grid is the same grid."""

    def test_the_ist_offset_is_the_19800s_the_defect_is_built_on(self):
        assert _ist_offset() == 19_800

    def test_one_minute_buckets_floor_identically_on_both_grids(self):
        assert _ist_offset() % 60 == 0
        assert [bucket_start(t, 60) for t in _grid()] == [
            _epoch_floor(t, 60) for t in _grid()
        ]

    def test_thirty_minutes_and_hourly_are_where_the_two_grids_diverge(self):
        """Negative control: the invariance is a property of 60, not of the lane.

        19,800 is a multiple of 1,800, so the *offset* alone would leave 30m
        alone; what moves 30m is that 09:15 is not a multiple of 1,800s from
        IST midnight. A grid agrees with the epoch floor only when both the
        offset and the open are whole multiples of its width.
        """
        assert _ist_offset() % 1800 == 0  # the offset does not rule 30m out...
        assert _open_seconds() % 1800 != 0  # ...the session open does
        opening = datetime.combine(_SESSION_DAY, MARKET_OPEN)
        for seconds in (1800, 3600):
            assert bucket_start(opening, seconds) == opening
            assert _epoch_floor(opening, seconds) != opening
        assert _epoch_floor(opening, 1800) == opening - timedelta(minutes=15)
        assert _epoch_floor(opening, 3600) == opening - timedelta(minutes=45)

    def test_every_intraday_width_anchors_at_the_session_open(self):
        """The grid as a list of numbers, so a fix that moves one width is caught.

        The test above loops the two widths the epoch floor got wrong, which
        passes just as happily if a third width regresses. Pinning all five
        positionally says it once: the bucket start of a 09:15 bar is 09:15, and
        the offset from the open is zero at every width the registry serves.
        """
        opening = datetime.combine(_SESSION_DAY, MARKET_OPEN)
        assert [
            (seconds, int((bucket_start(opening, seconds) - opening).total_seconds()))
            for seconds in _INTRADAY_SECONDS
        ] == [(60, 0), (300, 0), (900, 0), (1800, 0), (3600, 0)]

    def test_the_divergence_set_is_exactly_30m_and_hourly(self):
        """Which widths the lane moved, derived rather than remembered."""
        moved = [
            seconds
            for seconds in _INTRADAY_SECONDS
            if _ist_offset() % seconds or _open_seconds() % seconds
        ]
        assert moved == [1800, 3600]


class TestDensityWindowIsUnmoved:
    """``datalake_stats`` measures 09:16-15:29; that is still the interior."""

    def test_the_stats_band_is_the_one_minute_interior_of_the_session_grid(self):
        stats = _stats_module()
        interior = _interior()
        assert (dtime.fromisoformat(stats.INTERIOR_OPEN_STR),
                dtime.fromisoformat(stats.INTERIOR_CLOSE_STR)) == (
            interior[0].time(), interior[-1].time(),
        )
        assert len(interior) == 374  # 09:16..15:29

    def test_the_edges_stay_outside_the_band(self):
        """The two edge stamps are counted apart, never as density."""
        stats = _stats_module()
        interior = _interior()
        assert MARKET_OPEN < interior[0].time()
        assert interior[-1].time() < MARKET_CLOSE
        assert stats.OPEN_STR == MARKET_OPEN.isoformat()
        assert stats.CLOSE_STR == MARKET_CLOSE.isoformat()

    def test_the_session_close_emits_the_last_stamp_rather_than_minting_one(self):
        """The new close fires at 15:30, but the final bar is *stamped* 15:29.

        So the band gains no stamp, and the 15:29 bar the close now guarantees
        is already inside it.
        """
        stats = _stats_module()
        final = datetime.combine(_SESSION_DAY, dtime(15, 29, 59))
        assert bucket_start(final, 60) == datetime.combine(_SESSION_DAY, dtime(15, 29))
        assert dtime.fromisoformat(stats.INTERIOR_CLOSE_STR) >= bucket_start(
            final, 60
        ).time()


class _FakeInst:
    def __init__(self, symbol: str) -> None:
        self.symbol = symbol


def _gaps_with(tmp_path, skip: set, *, min_gap_stamps: int = 1):
    """A session built from the grid, minus *skip*, scanned at *min_gap_stamps*.

    The floor defaults to 1 — the module's own default — because the floor is no
    longer a library policy. A default above 1 is what made every symbol's Dhan
    tail invisible, and a hole that hides is worse than a hole that is reported
    and then judged.
    """
    stamps = _grid()
    store = ParquetStorage(tmp_path)
    store.upsert(pd.DataFrame([
        dict(symbol="RELIANCE", exchange="NSE", kind="equity", timeframe="1m",
             timestamp=ts, open=100, high=101, low=99, close=100, volume=10)
        for ts in stamps if ts not in skip
    ]))
    return GapDetector(store).scan(
        [_FakeInst("RELIANCE")],
        start=datetime.combine(_SESSION_DAY, MARKET_OPEN),
        end=datetime.combine(_SESSION_DAY, MARKET_CLOSE),
        timeframe="1m", bar_freq="1min", holidays=set(NSE_HOLIDAYS_2026),
        min_gap_stamps=min_gap_stamps,
    )


def _morning_hole(n: int) -> list[pd.Timestamp]:
    """``n`` consecutive interior minutes starting at 10:00, before the ceiling."""
    start = datetime.combine(_SESSION_DAY, dtime(10, 0))
    return [t for t in _interior() if start <= t.to_pydatetime() < start + timedelta(minutes=n)]


class TestFloorAgainstRealHoles:
    """The Dhan ceiling is classified. The floor only hides shorter real holes."""

    def test_the_derivation_itself(self):
        assert _dhan_tail_stamps() == 15

    def test_a_session_ending_at_15_14_is_complete(self, tmp_path):
        """15:15–15:29 is the close. It is counted, and it is not fetched."""
        tail = {t for t in _interior() if t.time() > DHAN_LAST_1M_STAMP}
        found = _gaps_with(tmp_path, tail)
        assert found.gaps == []
        assert found.close_missing_symbols == 1
        assert found.sub_threshold_stamps == 0

    def test_a_short_morning_hole_is_hidden_only_when_the_floor_asks(self, tmp_path):
        hole = _morning_hole(10)
        assert len(hole) == 10
        reported = _gaps_with(tmp_path, set(hole), min_gap_stamps=1)
        assert reported.gaps[0][1] == [
            (hole[0].to_pydatetime(), hole[-1].to_pydatetime())
        ]
        hidden = _gaps_with(tmp_path, set(hole), min_gap_stamps=FLOOR)
        assert hidden.gaps == []
        assert hidden.sub_threshold_stamps == 10

    def test_a_hole_that_clears_the_floor_is_still_reported(self, tmp_path):
        hole = _morning_hole(FLOOR)
        found = _gaps_with(tmp_path, set(hole), min_gap_stamps=FLOOR)
        assert found.gaps[0][1] == [
            (hole[0].to_pydatetime(), hole[-1].to_pydatetime())
        ]
        assert found.sub_threshold_stamps == 0

    def test_the_measured_lake_hole_would_survive_the_floor(self, tmp_path):
        """The one interior hole in the real 90-day repair window is 27 stamps."""
        measured_lake_hole_stamps = 27
        assert measured_lake_hole_stamps > FLOOR
        hole = _morning_hole(measured_lake_hole_stamps)
        found = _gaps_with(tmp_path, set(hole), min_gap_stamps=FLOOR)
        assert len(found.gaps) == 1
        assert found.sub_threshold_stamps == 0
