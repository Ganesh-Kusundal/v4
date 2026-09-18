"""Tests for GapDetector."""

from __future__ import annotations

from datetime import datetime
from datetime import time as dtime

import pandas as pd

from tradex_trading.datalake.gap_detector import GapDetector
from tradex_trading.datalake.parquet_storage import ParquetStorage


def _frame(rows):
    defaults = dict(symbol="RELIANCE", exchange="NSE", kind="equity",
                    timeframe="1m", volume=1000)
    for r in rows:
        for k, v in defaults.items():
            r.setdefault(k, v)
    return pd.DataFrame(rows)


class _FakeInst:
    def __init__(self, symbol):
        self.symbol = symbol


class TestGapDetector:
    def test_no_data_means_full_gap(self, tmp_path):
        store = ParquetStorage(tmp_path)
        detector = GapDetector(store)
        inst = _FakeInst("RELIANCE")
        gaps = detector.detect(
            [inst], start=datetime(2026, 7, 1), end=datetime(2026, 7, 31),
            timeframe="1m", bar_freq="1min",
        )
        assert len(gaps) == 1
        assert gaps[0][0].symbol == "RELIANCE"
        assert len(gaps[0][1]) > 0

    def test_complete_data_means_no_gap(self, tmp_path):
        store = ParquetStorage(tmp_path)
        inst = _FakeInst("RELIANCE")
        # Bars must be within market hours (09:15-15:30 IST) or read() strips
        # them; 60 contiguous minutes starting at 09:15 cover the window below.
        rows = [
            dict(timestamp=f"2026-07-01 09:{i:02d}:00", open=100, high=101, low=99, close=100)
            for i in range(15, 60)
        ] + [
            dict(timestamp=f"2026-07-01 10:{i:02d}:00", open=100, high=101, low=99, close=100)
            for i in range(0, 15)
        ]
        store.upsert(_frame(rows))
        detector = GapDetector(store)
        gaps = detector.detect(
            [inst], start=datetime(2026, 7, 1, 9, 15),
            end=datetime(2026, 7, 1, 10, 14), timeframe="1m", bar_freq="1min",
        )
        assert len(gaps) == 0 or all(len(ranges) == 0 for _, ranges in gaps)

    def test_missing_symbols(self, tmp_path):
        store = ParquetStorage(tmp_path)
        detector = GapDetector(store)
        insts = [_FakeInst("A"), _FakeInst("B")]
        missing = detector.missing_symbols(
            insts, start=datetime(2026, 7, 1), end=datetime(2026, 7, 31),
        )
        assert len(missing) == 2

    def test_last_stored_none_when_empty(self, tmp_path):
        store = ParquetStorage(tmp_path)
        detector = GapDetector(store)
        assert detector.last_stored("RELIANCE") is None

    def test_last_stored_returns_max_timestamp(self, tmp_path):
        store = ParquetStorage(tmp_path)
        rows = [
            dict(timestamp="2026-07-01 09:15:00", open=100, high=101, low=99, close=100),
            dict(timestamp="2026-07-01 09:30:00", open=100, high=101, low=99, close=100),
        ]
        store.upsert(_frame(rows))
        detector = GapDetector(store)
        last = detector.last_stored("RELIANCE")
        assert last is not None
        assert last.hour == 9 and last.minute == 30


# --- session-aware grid regression (backfill --skip-existing was a no-op) ---

def _session_rows(day: str, *, skip=()):
    """Full 30min session bars (09:15-15:30) for one day, minus `skip` times."""
    stamps = pd.date_range(f"{day} 09:15", f"{day} 15:30", freq="30min")
    return [
        dict(timestamp=str(ts), open=100, high=101, low=99, close=100)
        for ts in stamps
        if ts.time() not in skip
    ]


class TestSessionAwareDetection:
    def test_fully_synced_multiday_window_has_no_gaps(self, tmp_path):
        # Fri + Mon fully stored; window spans the weekend. Before the
        # session-aware fix, Sat/Sun calendar minutes were flagged missing.
        store = ParquetStorage(tmp_path)
        store.upsert(_frame(_session_rows("2026-07-03") + _session_rows("2026-07-06")))
        detector = GapDetector(store)
        gaps = detector.detect(
            [_FakeInst("RELIANCE")],
            start=datetime(2026, 7, 3, 9, 15),
            end=datetime(2026, 7, 6, 15, 30),
            timeframe="1m", bar_freq="30min",
        )
        assert gaps == []

    def test_holiday_excluded_via_holidays_arg(self, tmp_path):
        # Monday declared a holiday: its absence must not be flagged.
        store = ParquetStorage(tmp_path)
        store.upsert(_frame(_session_rows("2026-07-03")))
        detector = GapDetector(store)
        gaps = detector.detect(
            [_FakeInst("RELIANCE")],
            start=datetime(2026, 7, 3, 9, 15),
            end=datetime(2026, 7, 6, 15, 30),
            timeframe="1m", bar_freq="30min",
            holidays={datetime(2026, 7, 6).date()},
        )
        assert gaps == []

    def test_mid_session_hole_is_single_contiguous_range(self, tmp_path):
        # Missing two consecutive 30min stamps (11:15, 11:45) on Monday ->
        # one gap bounded to that day.
        store = ParquetStorage(tmp_path)
        hole = {dtime(11, 15), dtime(11, 45)}
        store.upsert(_frame(
            _session_rows("2026-07-03") + _session_rows("2026-07-06", skip=hole)
        ))
        detector = GapDetector(store)
        gaps = detector.detect(
            [_FakeInst("RELIANCE")],
            start=datetime(2026, 7, 3, 9, 15),
            end=datetime(2026, 7, 6, 15, 30),
            timeframe="1m", bar_freq="30min",
        )
        assert len(gaps) == 1
        inst, ranges = gaps[0]
        assert inst.symbol == "RELIANCE"
        assert ranges == [(datetime(2026, 7, 6, 11, 15), datetime(2026, 7, 6, 11, 45))]

    def test_min_gap_stamps_drops_noise_keeps_real_holes(self, tmp_path):
        # Friday entirely absent (real hole, 13 stamps) vs a 2-stamp noise
        # hole on Monday: floor of 3 keeps only the real gap.
        store = ParquetStorage(tmp_path)
        noise = {dtime(10, 45), dtime(11, 15)}
        store.upsert(_frame(_session_rows("2026-07-06", skip=noise)))
        detector = GapDetector(store)
        insts = [_FakeInst("RELIANCE")]

        exact = detector.detect(insts, start=datetime(2026, 7, 3, 9, 15),
                                end=datetime(2026, 7, 6, 15, 30),
                                timeframe="1m", bar_freq="30min")
        assert len(exact) == 1 and len(exact[0][1]) == 2

        floored = detector.detect(insts, start=datetime(2026, 7, 3, 9, 15),
                                  end=datetime(2026, 7, 6, 15, 30),
                                  timeframe="1m", bar_freq="30min",
                                  min_gap_stamps=3)
        assert len(floored) == 1
        # 09:15 / 15:15 are Friday's session EDGE stamps: they belong to the
        # edge classification, so the range is the interior span between them
        # (see TestSessionEdgeStamps).
        assert floored[0][1] == [(datetime(2026, 7, 3, 9, 45),
                                 datetime(2026, 7, 3, 14, 45))]


class TestSessionEdgeStamps:
    """Session-edge stamps are classified, never reported as data holes."""

    def test_edge_stamps_are_read_off_the_grid(self):
        """On a 30min grid a session ends at 15:15 — not MARKET_CLOSE."""
        from tradex_trading.datalake.gap_detector import _edge_stamps, _session_grid

        grid = _session_grid(datetime(2026, 7, 3, 9, 15), datetime(2026, 7, 3, 15, 30),
                             "30min", frozenset())
        opens, closes = _edge_stamps(grid)
        assert opens == {pd.Timestamp("2026-07-03 09:15")}
        assert closes == {pd.Timestamp("2026-07-03 15:15")}

    def test_open_bar_only_is_classified_not_gapped(self, tmp_path):
        """A symbol short only of the 09:15 open is not a data hole.

        This is the phantom gap: Dhan's intraday window is start-exclusive, so
        a session fetched from the open loses this bar while every other bar
        arrives. Counting it as a gap flagged all 500 symbols on the days the
        pipeline happened to fetch them that way.
        """
        store = ParquetStorage(tmp_path)
        store.upsert(_frame(_session_rows("2026-07-03", skip={dtime(9, 15)})))
        detector = GapDetector(store)
        insts = [_FakeInst("RELIANCE")]
        kwargs = dict(start=datetime(2026, 7, 3, 9, 15), end=datetime(2026, 7, 3, 15, 30),
                      timeframe="1m", bar_freq="30min")

        assert detector.detect(insts, **kwargs) == []
        found = detector.scan(insts, **kwargs)
        assert found.gaps == []
        assert found.edge_only == ("RELIANCE",)
        assert found.open_missing_symbols == 1
        assert found.close_missing_symbols == 0

    def test_close_bar_only_is_classified_not_gapped(self, tmp_path):
        store = ParquetStorage(tmp_path)
        store.upsert(_frame(_session_rows("2026-07-03", skip={dtime(15, 15)})))
        detector = GapDetector(store)
        found = detector.scan(
            [_FakeInst("RELIANCE")],
            start=datetime(2026, 7, 3, 9, 15), end=datetime(2026, 7, 3, 15, 30),
            timeframe="1m", bar_freq="30min",
        )
        assert found.gaps == []
        assert found.edge_only == ("RELIANCE",)
        assert found.close_missing_symbols == 1
        assert found.open_missing_symbols == 0

    def test_a_real_hole_still_reports_even_alongside_edge_stamps(self, tmp_path):
        """Classifying edges must not hide an interior hole next to one."""
        store = ParquetStorage(tmp_path)
        inst = _FakeInst("RELIANCE")
        store.upsert(_frame(_session_rows(
            "2026-07-03", skip={dtime(9, 15), dtime(11, 15), dtime(11, 45),
                                dtime(12, 15)},
        )))
        detector = GapDetector(store)
        found = detector.scan(
            [inst], start=datetime(2026, 7, 3, 9, 15),
            end=datetime(2026, 7, 3, 15, 30), timeframe="1m", bar_freq="30min",
        )
        assert found.gaps == [
            (inst, [(datetime(2026, 7, 3, 11, 15), datetime(2026, 7, 3, 12, 15))])
        ]
        assert found.open_missing_symbols == 1
        assert found.edge_only == ()

    def test_include_open_stamps_folds_them_back_in(self, tmp_path):
        """A repair run asks for the open bar explicitly, and gets it."""
        store = ParquetStorage(tmp_path)
        store.upsert(_frame(_session_rows("2026-07-03", skip={dtime(9, 15)})))
        detector = GapDetector(store)
        gaps = detector.detect(
            [_FakeInst("RELIANCE")],
            start=datetime(2026, 7, 3, 9, 15), end=datetime(2026, 7, 3, 15, 30),
            timeframe="1m", bar_freq="30min", include_open_stamps=True,
        )
        assert len(gaps) == 1
        assert gaps[0][1] == [(datetime(2026, 7, 3, 9, 15), datetime(2026, 7, 3, 9, 15))]

    def test_complete_symbol_is_counted_complete(self, tmp_path):
        store = ParquetStorage(tmp_path)
        store.upsert(_frame(_session_rows("2026-07-03")))
        found = GapDetector(store).scan(
            [_FakeInst("RELIANCE")],
            start=datetime(2026, 7, 3, 9, 15), end=datetime(2026, 7, 3, 15, 30),
            timeframe="1m", bar_freq="30min",
        )
        assert found.complete_symbols == 1
        assert found.gaps == [] and found.edge_only == ()


class TestSubThresholdVisibility:
    """Holes hiding under the min_gap_stamps floor are counted, not lost."""

    def test_floored_hole_is_reported(self, tmp_path):
        """The 2026-08-31 shape: a 14-stamp tail hole under a floor of 15."""
        store = ParquetStorage(tmp_path)
        skip = {dtime(11, 15), dtime(11, 45)}
        store.upsert(_frame(_session_rows("2026-07-06", skip=skip)))
        detector = GapDetector(store)
        found = detector.scan(
            [_FakeInst("RELIANCE")],
            start=datetime(2026, 7, 6, 9, 15), end=datetime(2026, 7, 6, 15, 30),
            timeframe="1m", bar_freq="30min", min_gap_stamps=3,
        )
        assert found.gaps == []
        assert found.sub_threshold_stamps == 2
        assert found.sub_threshold_symbols == ("RELIANCE",)

    def test_floor_that_nothing_hides_under_reports_zero(self, tmp_path):
        store = ParquetStorage(tmp_path)
        store.upsert(_frame(_session_rows("2026-07-06")))
        found = GapDetector(store).scan(
            [_FakeInst("RELIANCE")],
            start=datetime(2026, 7, 6, 9, 15), end=datetime(2026, 7, 6, 15, 30),
            timeframe="1m", bar_freq="30min", min_gap_stamps=3,
        )
        assert found.sub_threshold_stamps == 0
        assert found.sub_threshold_symbols == ()

    def test_gapped_symbols_property(self, tmp_path):
        store = ParquetStorage(tmp_path)
        found = GapDetector(store).scan(
            [_FakeInst("A"), _FakeInst("B")],
            start=datetime(2026, 7, 6, 9, 15), end=datetime(2026, 7, 6, 15, 30),
            timeframe="1m", bar_freq="30min",
        )
        assert found.gapped_symbols == 2


def test_tail_days_limits_scan_window(tmp_path):
    store = ParquetStorage(tmp_path)
    rows = [
        dict(timestamp=f"2026-07-01 09:{i:02d}:00", open=100, high=101, low=99, close=100)
        for i in range(15, 60)
    ] + [
        dict(timestamp=f"2026-07-01 10:{i:02d}:00", open=100, high=101, low=99, close=100)
        for i in range(0, 15)
    ]
    store.upsert(_frame(rows))
    detector = GapDetector(store)
    inst = _FakeInst("RELIANCE")
    full = detector.detect(
        [inst], start=datetime(2026, 7, 1, 9, 15),
        end=datetime(2026, 7, 1, 10, 14), timeframe="1m", bar_freq="1min",
    )
    tailed = detector.detect(
        [inst], start=datetime(2026, 7, 1, 9, 15),
        end=datetime(2026, 7, 1, 10, 14), timeframe="1m", bar_freq="1min",
        tail_days=7,
    )
    assert full == tailed == []


def test_parallel_detect_matches_sequential(tmp_path):
    store = ParquetStorage(tmp_path)
    detector = GapDetector(store)
    insts = [_FakeInst("A"), _FakeInst("B"), _FakeInst("C")]
    kwargs = dict(
        start=datetime(2026, 7, 1), end=datetime(2026, 7, 31),
        timeframe="1m", bar_freq="1min",
    )
    seq = {i.symbol: r for i, r in detector.detect(insts, max_workers=1, **kwargs)}
    par = {i.symbol: r for i, r in detector.detect(insts, max_workers=4, **kwargs)}
    assert seq == par
