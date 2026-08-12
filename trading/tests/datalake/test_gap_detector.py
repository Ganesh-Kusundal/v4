"""Tests for GapDetector."""

from __future__ import annotations

from datetime import datetime

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
        rows = [
            dict(timestamp=f"2026-07-01 09:{i:02d}:00", open=100, high=101, low=99, close=100)
            for i in range(60)
        ]
        store.upsert(_frame(rows))
        detector = GapDetector(store)
        inst = _FakeInst("RELIANCE")
        gaps = detector.detect(
            [inst], start=datetime(2026, 7, 1, 9, 0),
            end=datetime(2026, 7, 1, 9, 59), timeframe="1m", bar_freq="1min",
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
