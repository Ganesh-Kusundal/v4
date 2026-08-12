# Datalake Implementation Plan

> **For agentic workers:** Use superpowers:subagent-driven-development or superpowers:executing-plans.

**Goal:** Port nTrade's Hive-partitioned Parquet datalake to TradeX v4, reusing the ParallelHistoryFetcher we just built.

**Architecture:** Three new files in `datalake/`: `ParquetStorage` (Hive-partitioned upsert store), `GapDetector` (incremental backfill awareness), `backfill.py` (batch script). Reuses existing `ParallelHistoryFetcher`.

**Tech Stack:** pyarrow, pandas, duckdb (all already installed).

---

## File Structure

```
CREATE:
  trading/src/tradex_trading/datalake/parquet_storage.py  — Hive-partitioned store
  trading/src/tradex_trading/datalake/gap_detector.py     — Missing range detection
  trading/scripts/backfill_parquet.py                      — Batch backfill script

MODIFY:
  trading/src/tradex_trading/datalake/__init__.py          — Export new symbols

TESTS:
  trading/tests/datalake/test_parquet_storage.py           — Storage tests
  trading/tests/datalake/test_gap_detector.py              — Gap detection tests
```

---

### Task 1: ParquetStorage

**Files:**
- Create: `trading/src/tradex_trading/datalake/parquet_storage.py`
- Test: `trading/tests/datalake/test_parquet_storage.py`

Adapted from nTrade's `ParquetStorage` — Hive-partitioned layout with upsert semantics.

- [ ] **Step 1: Write tests**

```python
# trading/tests/datalake/test_parquet_storage.py
"""Tests for ParquetStorage — Hive-partitioned Parquet store."""

from __future__ import annotations

from datetime import datetime

import pandas as pd
import pytest

from tradex_trading.datalake.parquet_storage import ParquetStorage


def _frame(rows: list[dict]) -> pd.DataFrame:
    defaults = dict(symbol="RELIANCE", exchange="NSE", kind="equity",
                    timeframe="1m", volume=1000)
    for r in rows:
        for k, v in defaults.items():
            r.setdefault(k, v)
    return pd.DataFrame(rows)


class TestParquetStorage:
    def test_upsert_and_read(self, tmp_path):
        store = ParquetStorage(tmp_path)
        df = _frame([
            dict(timestamp="2026-07-01 09:15:00", open=100, high=101, low=99, close=100),
            dict(timestamp="2026-07-01 09:16:00", open=100, high=102, low=99, close=101),
        ])
        written = store.upsert(df)
        assert written == 2

        result = store.read(symbols=["RELIANCE"])
        assert len(result) == 2

    def test_upsert_is_idempotent(self, tmp_path):
        """Re-upserting same data replaces overlapping rows, not duplicates."""
        store = ParquetStorage(tmp_path)
        df = _frame([
            dict(timestamp="2026-07-01 09:15:00", open=100, high=101, low=99, close=100),
        ])
        store.upsert(df)
        store.upsert(df)  # same data again
        result = store.read(symbols=["RELIANCE"])
        assert len(result) == 1  # not 2

    def test_upsert_replaces_overlapping(self, tmp_path):
        """New data for same timestamp replaces old values."""
        store = ParquetStorage(tmp_path)
        df1 = _frame([dict(timestamp="2026-07-01 09:15:00", open=100, high=101, low=99, close=100)])
        df2 = _frame([dict(timestamp="2026-07-01 09:15:00", open=200, high=201, low=199, close=200)])
        store.upsert(df1)
        store.upsert(df2)
        result = store.read(symbols=["RELIANCE"])
        assert len(result) == 1
        assert float(result.iloc[0]["open"]) == 200.0

    def test_read_with_date_filter(self, tmp_path):
        store = ParquetStorage(tmp_path)
        df = _frame([
            dict(timestamp="2026-07-01 09:15:00", open=100, high=101, low=99, close=100),
            dict(timestamp="2026-08-01 09:15:00", open=200, high=201, low=199, close=200),
        ])
        store.upsert(df)
        result = store.read(symbols=["RELIANCE"],
                           start=datetime(2026, 7, 15), end=datetime(2026, 8, 15))
        assert len(result) == 1

    def test_symbols_list(self, tmp_path):
        store = ParquetStorage(tmp_path)
        df = _frame([dict(timestamp="2026-07-01 09:15:00", open=100, high=101, low=99, close=100)])
        store.upsert(df)
        assert "RELIANCE" in store.symbols()

    def test_empty_upsert_returns_zero(self, tmp_path):
        store = ParquetStorage(tmp_path)
        assert store.upsert(pd.DataFrame()) == 0

    def test_read_empty_store(self, tmp_path):
        store = ParquetStorage(tmp_path)
        result = store.read(symbols=["NONEXISTENT"])
        assert result.empty

    def test_clear(self, tmp_path):
        store = ParquetStorage(tmp_path)
        df = _frame([dict(timestamp="2026-07-01 09:15:00", open=100, high=101, low=99, close=100)])
        store.upsert(df)
        store.clear()
        assert store.read(symbols=["RELIANCE"]).empty
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `python -m pytest trading/tests/datalake/test_parquet_storage.py -v`
Expected: FAIL — `ParquetStorage` not importable

- [ ] **Step 3: Implement ParquetStorage**

Adapt from nTrade's `parquet_store.py` — keep Hive layout, upsert, DuckDB scan.

- [ ] **Step 4: Run tests**

Run: `python -m pytest trading/tests/datalake/test_parquet_storage.py -v`
Expected: ALL PASS

- [ ] **Step 5: Commit**

---

### Task 2: GapDetector

**Files:**
- Create: `trading/src/tradex_trading/datalake/gap_detector.py`
- Test: `trading/tests/datalake/test_gap_detector.py`

- [ ] **Step 1: Write tests**

```python
# trading/tests/datalake/test_gap_detector.py
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
        gaps = detector.detect([inst], start=datetime(2026, 7, 1), end=datetime(2026, 7, 31),
                              timeframe="1m", bar_freq="1min")
        assert len(gaps) == 1
        assert gaps[0][0].symbol == "RELIANCE"
        assert len(gaps[0][1]) > 0  # has missing ranges

    def test_complete_data_means_no_gap(self, tmp_path):
        store = ParquetStorage(tmp_path)
        # Fill every minute for 1 hour
        rows = [dict(timestamp=f"2026-07-01 09:{i:02d}:00", open=100, high=101, low=99, close=100)
                for i in range(60)]
        store.upsert(_frame(rows))
        detector = GapDetector(store)
        inst = _FakeInst("RELIANCE")
        gaps = detector.detect([inst], start=datetime(2026, 7, 1, 9, 0),
                              end=datetime(2026, 7, 1, 9, 59), timeframe="1m", bar_freq="1min")
        # Should have no gaps (all minutes covered)
        assert len(gaps) == 0 or all(len(ranges) == 0 for _, ranges in gaps)

    def test_missing_symbols(self, tmp_path):
        store = ParquetStorage(tmp_path)
        detector = GapDetector(store)
        insts = [_FakeInst("A"), _FakeInst("B")]
        missing = detector.missing_symbols(insts, start=datetime(2026, 7, 1),
                                           end=datetime(2026, 7, 31))
        assert len(missing) == 2  # both missing

    def test_last_stored_none_when_empty(self, tmp_path):
        store = ParquetStorage(tmp_path)
        detector = GapDetector(store)
        assert detector.last_stored("RELIANCE") is None
```

- [ ] **Step 2: Run tests to verify they fail**
- [ ] **Step 3: Implement GapDetector**

Adapt from nTrade's `gap_detector.py`.

- [ ] **Step 4: Run tests**
- [ ] **Step 5: Commit**

---

### Task 3: Backfill Script

**Files:**
- Create: `trading/scripts/backfill_parquet.py`

- [ ] **Step 1: Implement backfill script**

Uses `ParallelHistoryFetcher` + `ParquetStorage` + `GapDetector`.
Batch fetch → convert HistoricalSeries to DataFrame → upsert.

- [ ] **Step 2: Smoke test with paper broker**
- [ ] **Step 3: Commit**

---

### Task 4: Update Exports

- [ ] **Step 1: Update `datalake/__init__.py`**

Add `ParquetStorage`, `GapDetector` to exports.

- [ ] **Step 2: Run full datalake test suite**
- [ ] **Step 3: Commit**
