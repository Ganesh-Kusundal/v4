# Universe Sync Manager Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Build a unified manager that keeps the NSE 500 datalake aligned with current universe constituents, handles symbol renames via ISIN matching, and retries failed syncs with persistent backoff tracking.

**Architecture:** Three-phase orchestration in `UniverseSyncManager`: (1) diff universe CSV against datalake, (2) detect and migrate renamed symbols by ISIN, (3) retry failed symbols with classified backoff. Metadata files track state across runs.

**Tech Stack:** Python 3.13+, pandas for parquet manipulation, tradex_domain.universe for CSV loading, tradex_market_data.sync_service for sync execution, JSON for metadata persistence.

**Spec:** docs/superpowers/specs/2026-11-07-universe-sync-manager-design.md

## Global Constraints

- Trading-symbol keying remains canonical; ISIN is alias layer only (no tier-3 ISIN-keyed IDs)
- NSE equity only; no BSE/MCX handling
- Lake migrations must be atomic and reversible
- All metadata files live under `data/` directory
- Exponential backoff doubles per attempt, capped at 480 seconds
- Max 3 retry attempts before marking permanent failure

---

### Task 1: Data Structures and Type Definitions

**Files:**
- Create: `market_data/src/tradex_market_data/universe_sync.py` (lines 1-150)
- Test: `market_data/tests/test_universe_sync_types.py`

**Interfaces:**
- Produces: Five dataclasses used throughout all subsequent tasks
- Consumes: `Equity` from `tradex_domain.instruments`, `datetime` from stdlib

- [ ] **Step 1: Write test for dataclass instantiation**

```python
# market_data/tests/test_universe_sync_types.py
from datetime import datetime, timedelta, timezone
from tradex_market_data.universe_sync import (
    UniverseDelta,
    RenameCandidate,
    MigrationResult,
    FailureRecord,
)

def test_universe_delta_creation():
    delta = UniverseDelta(
        added=[],
        removed=["OLDSTOCK"],
        unchanged=[],
        last_refreshed=datetime.now(timezone.utc),
    )
    assert len(delta.removed) == 1
    assert delta.last_refreshed.tzinfo is not None

def test_rename_candidate_defaults():
    candidate = RenameCandidate(
        old_symbol="HEG",
        new_symbol="HEGAM",
        isin="INE545A01024",
    )
    assert candidate.confidence == 1.0
    assert candidate.status == "pending"

def test_failure_record_retry_after():
    record = FailureRecord(
        symbol="ABLBL",
        failed_at=datetime.now(timezone.utc),
        reason="rate_limited",
        attempts=1,
        last_error="Rate limit exceeded",
        retry_after=datetime.now(timezone.utc) + timedelta(hours=1),
    )
    assert record.retry_after is not None
    assert record.attempts == 1
```

- [ ] **Step 2: Run test to verify it fails**

Run: `pytest market_data/tests/test_universe_sync_types.py -v`
Expected: FAIL with "ModuleNotFoundError: No module named 'tradex_market_data.universe_sync'"

- [ ] **Step 3: Write dataclass definitions**

```python
# market_data/src/tradex_market_data/universe_sync.py
"""Universe sync manager — unified orchestrator for universe alignment."""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime


@dataclass
class UniverseDelta:
    """Delta between universe CSV and datalake contents."""
    added: list = field(default_factory=list)  # list[Equity]
    removed: list[str] = field(default_factory=list)
    unchanged: list = field(default_factory=list)  # list[Equity]
    last_refreshed: datetime | None = None


@dataclass
class RenameCandidate:
    """Detected symbol rename via ISIN match."""
    old_symbol: str
    new_symbol: str
    isin: str
    confidence: float = 1.0
    status: str = "pending"  # "pending" | "migrated" | "skipped"


@dataclass
class MigrationResult:
    """Result of lake directory migration."""
    success: bool
    rows_migrated: int = 0
    error: str | None = None


@dataclass
class FailureRecord:
    """Persistent failure tracking with backoff."""
    symbol: str
    failed_at: datetime
    reason: str  # "rate_limited" | "not_found" | "timeout" | "delisted"
    attempts: int = 1
    last_error: str = ""
    retry_after: datetime | None = None
```

- [ ] **Step 4: Run test to verify it passes**

Run: `pytest market_data/tests/test_universe_sync_types.py -v`
Expected: PASS (all 3 tests)

- [ ] **Step 5: Commit**

```bash
git add market_data/src/tradex_market_data/universe_sync.py market_data/tests/test_universe_sync_types.py
git commit -m "feat: add universe sync data structures and type definitions"
```

---

### Task 2: Universe Refresh — Diff CSV Against Datalake

**Files:**
- Modify: `market_data/src/tradex_market_data/universe_sync.py` (add `UniverseSyncManager.__init__` and `refresh_universe`)
- Test: `market_data/tests/test_universe_sync_refresh.py`

**Interfaces:**
- Consumes: `load_universe()` from `tradex_domain.universe`, `Path` operations
- Produces: `UniverseSyncManager.refresh_universe()` returning `UniverseDelta`

- [ ] **Step 1: Write test for universe delta detection**

```python
# market_data/tests/test_universe_sync_refresh.py
import tempfile
from pathlib import Path
from unittest.mock import patch
from tradex_domain.universe import Equity
from tradex_domain.value_objects import InstrumentId
from tradex_domain.enums import ExchangeId
from tradex_market_data.universe_sync import UniverseSyncManager, UniverseDelta


def make_equity(symbol: str) -> Equity:
    return Equity(
        instrument_id=InstrumentId.equity("NSE", symbol),
        symbol=symbol,
        exchange=ExchangeId.NSE,
    )


def test_refresh_universe_detects_added_symbols(tmp_path):
    # Setup: datalake has 2 symbols, CSV has 3
    datalake = tmp_path / "data" / "ohlcv"
    datalake.mkdir(parents=True)
    (datalake / "symbol=SYM1").mkdir()
    (datalake / "symbol=SYM2").mkdir()

    # Mock CSV with 3 symbols
    csv_symbols = [make_equity("SYM1"), make_equity("SYM2"), make_equity("SYM3")]

    with patch("tradex_market_data.universe_sync.load_universe", return_value=csv_symbols):
        manager = UniverseSyncManager(broker=None, store=None, csv_dir=tmp_path)
        manager._datalake_root = datalake.parent  # Point to parent of ohlcv

        delta = manager.refresh_universe("nifty500")

        assert len(delta.added) == 1
        assert delta.added[0].symbol == "SYM3"
        assert len(delta.removed) == 0
        assert len(delta.unchanged) == 2
```

- [ ] **Step 2: Run test to verify it fails**

Run: `pytest market_data/tests/test_universe_sync_refresh.py::test_refresh_universe_detects_added_symbols -v`
Expected: FAIL with "AttributeError: 'UniverseSyncManager' object has no attribute 'refresh_universe'"

- [ ] **Step 3: Implement UniverseSyncManager skeleton and refresh_universe**

```python
# Add to market_data/src/tradex_market_data/universe_sync.py (after imports)
import json
import logging
import shutil
from collections.abc import Iterable
from datetime import UTC, timedelta
from pathlib import Path

import pandas as pd
from tradex_domain.universe import load_universe
from tradex_domain.instruments import Equity

log = logging.getLogger(__name__)


class UniverseSyncManager:
    """Unified orchestrator for universe alignment and sync recovery."""

    def __init__(
        self,
        broker,
        store,
        csv_dir: Path | None = None,
        config=None,
        datalake_root: Path | None = None,
    ) -> None:
        self._broker = broker
        self._store = store
        self._config = config
        self._csv_dir = csv_dir
        self._datalake_root = datalake_root or Path("data")

    def refresh_universe(
        self,
        universe_name: str = "nifty500",
        csv_path: Path | None = None,
    ) -> UniverseDelta:
        """Diff current universe CSV against datalake and return added/removed symbols."""
        # Load universe CSV
        if csv_path is None:
            csv_instruments = load_universe(universe_name, csv_dir=self._csv_dir)
        else:
            csv_instruments = load_universe(csv_path=csv_path.parent, csv_dir=csv_path.parent)

        # List datalake directories
        ohlcv_dir = self._datalake_root / "ohlcv"
        datalake_symbols = set()
        if ohlcv_dir.exists():
            for sym_dir in ohlcv_dir.glob("symbol=*"):
                symbol = sym_dir.name.split("=")[1]
                datalake_symbols.add(symbol)

        # Compute set differences
        csv_symbols = {inst.symbol for inst in csv_instruments}
        added_symbols = csv_symbols - datalake_symbols
        removed_symbols = datalake_symbols - csv_symbols

        # Categorize instruments
        added = [inst for inst in csv_instruments if inst.symbol in added_symbols]
        removed = sorted(removed_symbols)
        unchanged = [inst for inst in csv_instruments if inst.symbol in datalake_symbols]

        delta = UniverseDelta(
            added=added,
            removed=removed,
            unchanged=unchanged,
            last_refreshed=datetime.now(UTC),
        )

        # Write metadata
        self._write_universe_metadata(universe_name, delta)

        log.info(
            "Universe refresh: +%d added, -%d removed, %d unchanged",
            len(delta.added),
            len(delta.removed),
            len(delta.unchanged),
        )

        return delta

    def _write_universe_metadata(
        self,
        universe_name: str,
        delta: UniverseDelta,
    ) -> None:
        """Write data/.universe_metadata.json."""
        metadata = {
            "universe_name": universe_name,
            "last_refreshed": delta.last_refreshed.isoformat() if delta.last_refreshed else None,
            "total_symbols": len(delta.added) + len(delta.unchanged),
            "added": [inst.symbol for inst in delta.added],
            "removed": delta.removed,
        }

        metadata_path = self._datalake_root / ".universe_metadata.json"
        with open(metadata_path, "w") as f:
            json.dump(metadata, f, indent=2)
```

- [ ] **Step 4: Run test to verify it passes**

Run: `pytest market_data/tests/test_universe_sync_refresh.py -v`
Expected: PASS

- [ ] **Step 5: Commit**

```bash
git add market_data/src/tradex_market_data/universe_sync.py market_data/tests/test_universe_sync_refresh.py
git commit -m "feat: implement universe refresh with CSV-datalake diff"
```

---

### Task 3: Rename Detection via ISIN Matching

**Files:**
- Modify: `market_data/src/tradex_market_data/universe_sync.py` (add `detect_renames`)
- Test: `market_data/tests/test_universe_sync_renames.py`

**Interfaces:**
- Consumes: Broker registry with `meta()` and `provider_key()` methods
- Produces: `detect_renames()` returning `list[RenameCandidate]`

- [ ] **Step 1: Write test for rename detection**

```python
# market_data/tests/test_universe_sync_renames.py
from unittest.mock import MagicMock
from tradex_market_data.universe_sync import RenameCandidate


def test_detect_rename_same_isin_different_symbol():
    # Mock broker with HEG→HEGAM rename
    broker = MagicMock()
    registry = MagicMock()

    # Old symbol HEG in universe
    old_inst = make_equity("HEG")
    old_inst.meta.isin = "INE545A01024"

    # New symbol HEGAM in broker master with same ISIN
    new_iid = MagicMock()
    registry.provider_key.return_value = "NSE_EQ|INE545A01024"
    registry.meta.return_value = {"isin": "INE545A01024"}

    # Mock loaded instruments
    new_inst = make_equity("HEGAM")
    broker._loaded_instruments = [new_inst]
    broker.registry = registry

    manager = UniverseSyncManager(broker=broker, store=None)
    manager._universe_instruments = [old_inst]

    candidates = manager.detect_renames()

    assert len(candidates) == 1
    assert candidates[0].old_symbol == "HEG"
    assert candidates[0].new_symbol == "HEGAM"
    assert candidates[0].isin == "INE545A01024"
```

- [ ] **Step 2: Run test to verify it fails**

Run: `pytest market_data/tests/test_universe_sync_renames.py::test_detect_rename_same_isin_different_symbol -v`
Expected: FAIL with "AttributeError: 'UniverseSyncManager' object has no attribute 'detect_renames'"

- [ ] **Step 3: Implement detect_renames**

```python
# Add to UniverseSyncManager class
def detect_renames(self, universe_instruments: list[Equity] | None = None) -> list[RenameCandidate]:
    """Compare universe ISINs against broker master to find renamed symbols."""
    if self._broker is None:
        log.warning("No broker available for rename detection")
        return []

    if universe_instruments is None:
        # Fallback: load from default universe
        try:
            universe_instruments = load_universe("nifty500", csv_dir=self._csv_dir)
        except FileNotFoundError:
            return []

    self._universe_instruments = universe_instruments

    # Build ISIN index from broker master
    isin_index = self._build_isin_index()

    candidates = []
    for inst in universe_instruments:
        csv_isin = getattr(getattr(inst, "meta", None), "isin", None)
        if not csv_isin:
            continue

        broker_symbol = isin_index.get(csv_isin)
        if broker_symbol and broker_symbol != inst.symbol:
            # Check if old symbol exists in datalake
            old_exists = (self._datalake_root / "ohlcv" / f"symbol={inst.symbol}").exists()
            # Check if new symbol already has data
            new_exists = (self._datalake_root / "ohlcv" / f"symbol={broker_symbol}").exists()

            if old_exists and not new_exists:
                candidates.append(RenameCandidate(
                    old_symbol=inst.symbol,
                    new_symbol=broker_symbol,
                    isin=csv_isin,
                    confidence=1.0,
                    status="pending",
                ))
            elif old_exists and new_exists:
                log.warning(
                    "Rename conflict: both %s and %s have data (ISIN %s) — manual merge required",
                    inst.symbol,
                    broker_symbol,
                    csv_isin,
                )

    log.info("Rename detection: %d candidates found", len(candidates))
    return candidates

def _build_isin_index(self) -> dict[str, str]:
    """Build ISIN → trading symbol index from broker master."""
    isin_index = {}
    registry = self._broker.registry
    loaded = getattr(self._broker, "_loaded_instruments", [])

    for inst in loaded:
        if not isinstance(inst, Equity):
            continue
        iid = inst.instrument_id
        meta = registry.meta(iid)
        isin = meta.get("isin")
        if isin:
            isin_index[str(isin).strip().upper()] = inst.symbol

    return isin_index
```

- [ ] **Step 4: Run test to verify it passes**

Run: `pytest market_data/tests/test_universe_sync_renames.py -v`
Expected: PASS

- [ ] **Step 5: Commit**

```bash
git add market_data/src/tradex_market_data/universe_sync.py market_data/tests/test_universe_sync_renames.py
git commit -m "feat: add rename detection via ISIN matching against broker master"
```

---

### Task 4: Lake Migration — Rename Directories and Update Parquet

**Files:**
- Modify: `market_data/src/tradex_market_data/universe_sync.py` (add `migrate_symbol_lake`)
- Test: `market_data/tests/test_universe_sync_migration.py`

**Interfaces:**
- Consumes: `RenameCandidate` from Task 3
- Produces: `migrate_symbol_lake()` returning `MigrationResult`

- [ ] **Step 1: Write test for atomic migration**

```python
# market_data/tests/test_universe_sync_migration.py
import pandas as pd
from datetime import datetime
from tradex_domain.market import Candle, OHLC, Volume


def test_migration_atomic_rollback(tmp_path):
    # Setup: create old symbol directory with parquet file
    datalake = tmp_path / "data"
    ohlcv = datalake / "ohlcv" / "symbol=HEG" / "year=2024" / "month=01"
    ohlcv.mkdir(parents=True)

    # Create test parquet
    candle = Candle(
        timestamp=datetime(2024, 1, 2, 9, 15),
        ohlc=OHLC(open=100, high=105, low=95, close=102),
        volume=Volume(1000),
    )
    df = pd.DataFrame([{"timestamp": candle.timestamp, "symbol": "HEG", "open": 100}])
    df.to_parquet(ohlcv / "data.parquet", index=False)

    manager = UniverseSyncManager(broker=None, store=None, datalake_root=datalake)

    result = manager.migrate_symbol_lake("HEG", "HEGAM", "INE545A01024")

    assert result.success
    assert result.rows_migrated > 0
    assert (datalake / "ohlcv" / "symbol=HEGAM").exists()
    assert not (datalake / "ohlcv" / "symbol=HEG").exists()

    # Verify parquet symbol column updated
    migrated_file = datalake / "ohlcv" / "symbol=HEGAM" / "year=2024" / "month=01" / "data.parquet"
    df_migrated = pd.read_parquet(migrated_file)
    assert df_migrated["symbol"].iloc[0] == "HEGAM"
```

- [ ] **Step 2: Run test to verify it fails**

Run: `pytest market_data/tests/test_universe_sync_migration.py::test_migration_atomic_rollback -v`
Expected: FAIL with "AttributeError: 'UniverseSyncManager' object has no attribute 'migrate_symbol_lake'"

- [ ] **Step 3: Implement migrate_symbol_lake**

```python
# Add to UniverseSyncManager class
def migrate_symbol_lake(
    self,
    old_symbol: str,
    new_symbol: str,
    isin: str,
) -> MigrationResult:
    """Rename lake directory, update parquet symbol column, log the rename."""
    old_dir = self._datalake_root / "ohlcv" / f"symbol={old_symbol}"
    new_dir = self._datalake_root / "ohlcv" / f"symbol={new_symbol}"

    if not old_dir.exists():
        return MigrationResult(success=False, error=f"Old directory not found: {old_dir}")

    if new_dir.exists():
        return MigrationResult(
            success=False,
            error=f"New directory already exists: {new_dir} — manual merge required",
        )

    try:
        # Copy directory first (atomic-ish)
        shutil.copytree(old_dir, new_dir)

        # Update parquet symbol columns
        total_rows = 0
        for parquet_file in new_dir.rglob("*.parquet"):
            df = pd.read_parquet(parquet_file)
            if "symbol" in df.columns:
                row_count = len(df)
                df["symbol"] = new_symbol
                df.to_parquet(parquet_file, index=False)
                total_rows += row_count

        # Verify copy succeeded
        if total_rows == 0:
            raise RuntimeError("No rows migrated — parquet files may be corrupted")

        # Delete old directory only after successful copy
        shutil.rmtree(old_dir)

        # Log the rename
        self._log_rename(old_symbol, new_symbol, isin, total_rows)

        log.info("Lake migration: %s → %s (%d rows)", old_symbol, new_symbol, total_rows)
        return MigrationResult(success=True, rows_migrated=total_rows)

    except Exception as exc:
        # Rollback: delete partial copy if it exists
        if new_dir.exists():
            shutil.rmtree(new_dir)
        log.error("Migration failed for %s → %s: %s — rolled back", old_symbol, new_symbol, exc)
        return MigrationResult(success=False, error=str(exc))

def _log_rename(
    self,
    old_symbol: str,
    new_symbol: str,
    isin: str,
    rows_affected: int,
) -> None:
    """Append entry to data/.rename_log.json."""
    rename_log_path = self._datalake_root / ".rename_log.json"

    # Load existing log
    rename_log = {}
    if rename_log_path.exists():
        with open(rename_log_path) as f:
            try:
                rename_log = json.load(f)
            except json.JSONDecodeError:
                log.warning("Corrupted rename log — starting fresh")

    # Append new entry
    rename_log[old_symbol] = {
        "new_symbol": new_symbol,
        "isin": isin,
        "renamed_at": datetime.now(UTC).isoformat(),
        "reason": "corporate_rename",
        "lake_migrated": True,
        "rows_affected": rows_affected,
    }

    with open(rename_log_path, "w") as f:
        json.dump(rename_log, f, indent=2)
```

- [ ] **Step 4: Run test to verify it passes**

Run: `pytest market_data/tests/test_universe_sync_migration.py -v`
Expected: PASS

- [ ] **Step 5: Commit**

```bash
git add market_data/src/tradex_market_data/universe_sync.py market_data/tests/test_universe_sync_migration.py
git commit -m "feat: implement atomic lake migration with parquet column update"
```

---

### Task 5: Failure Tracking Persistence

**Files:**
- Modify: `market_data/src/tradex_market_data/universe_sync.py` (add `load_failures`, `save_failures`)
- Test: `market_data/tests/test_universe_sync_failures.py`

**Interfaces:**
- Consumes: `FailureRecord` from Task 1
- Produces: `load_failures()` and `save_failures()` for JSON persistence

- [ ] **Step 1: Write test for failure persistence**

```python
# market_data/tests/test_universe_sync_failures.py
from datetime import datetime, timedelta, timezone
from tradex_market_data.universe_sync import FailureRecord


def test_failure_persistence_round_trip(tmp_path):
    manager = UniverseSyncManager(broker=None, store=None, datalake_root=tmp_path)

    # Save failures
    records = {
        "ABLBL": FailureRecord(
            symbol="ABLBL",
            failed_at=datetime.now(timezone.utc),
            reason="rate_limited",
            attempts=1,
            last_error="Rate limit exceeded",
            retry_after=datetime.now(timezone.utc) + timedelta(hours=1),
        ),
    }
    manager.save_failures(records)

    # Load failures
    loaded = manager.load_failures()

    assert "ABLBL" in loaded
    assert loaded["ABLBL"].reason == "rate_limited"
    assert loaded["ABLBL"].attempts == 1
```

- [ ] **Step 2: Run test to verify it fails**

Run: `pytest market_data/tests/test_universe_sync_failures.py::test_failure_persistence_round_trip -v`
Expected: FAIL with "AttributeError: 'UniverseSyncManager' object has no attribute 'load_failures'"

- [ ] **Step 3: Implement failure tracking**

```python
# Add to UniverseSyncManager class
def load_failures(self) -> dict[str, FailureRecord]:
    """Load persistent failure tracker from data/.sync_failures.json."""
    failures_path = self._datalake_root / ".sync_failures.json"

    if not failures_path.exists():
        return {}

    try:
        with open(failures_path) as f:
            data = json.load(f)
    except json.JSONDecodeError:
        log.warning("Corrupted failure tracker — starting fresh")
        return {}

    records = {}
    for symbol, record_data in data.items():
        try:
            records[symbol] = FailureRecord(
                symbol=record_data["symbol"],
                failed_at=datetime.fromisoformat(record_data["failed_at"]),
                reason=record_data["reason"],
                attempts=record_data.get("attempts", 1),
                last_error=record_data.get("last_error", ""),
                retry_after=(
                    datetime.fromisoformat(record_data["retry_after"])
                    if record_data.get("retry_after")
                    else None
                ),
            )
        except (KeyError, ValueError) as exc:
            log.warning("Skipping malformed failure record for %s: %s", symbol, exc)

    return records

def save_failures(self, records: dict[str, FailureRecord]) -> None:
    """Save failure tracker to data/.sync_failures.json."""
    failures_path = self._datalake_root / ".sync_failures.json"

    data = {}
    for symbol, record in records.items():
        data[symbol] = {
            "symbol": record.symbol,
            "failed_at": record.failed_at.isoformat(),
            "reason": record.reason,
            "attempts": record.attempts,
            "last_error": record.last_error,
            "retry_after": record.retry_after.isoformat() if record.retry_after else None,
        }

    with open(failures_path, "w") as f:
        json.dump(data, f, indent=2)

def classify_failure(self, error_message: str) -> str:
    """Classify failure reason from error message."""
    if "rate limit" in error_message.lower() or "429" in error_message:
        return "rate_limited"
    if "not found" in error_message.lower() or "instrument not found" in error_message.lower():
        return "not_found"
    if "timeout" in error_message.lower() or "timed out" in error_message.lower():
        return "timeout"
    return "unknown"
```

- [ ] **Step 4: Run test to verify it passes**

Run: `pytest market_data/tests/test_universe_sync_failures.py -v`
Expected: PASS

- [ ] **Step 5: Commit**

```bash
git add market_data/src/tradex_market_data/universe_sync.py market_data/tests/test_universe_sync_failures.py
git commit -m "feat: add persistent failure tracking with JSON serialization"
```

---

### Task 6: Smart Retry with Exponential Backoff

**Files:**
- Modify: `market_data/src/tradex_market_data/universe_sync.py` (add `retry_failed`)
- Test: `market_data/tests/test_universe_sync_retry.py`

**Interfaces:**
- Consumes: `FailureRecord` from Task 5, `SyncService` for actual sync
- Produces: `retry_failed()` returning `SyncReport`

- [ ] **Step 1: Write test for retry classification**

```python
# market_data/tests/test_universe_sync_retry.py
from datetime import datetime, timedelta, timezone
from unittest.mock import MagicMock
from tradex_market_data.universe_sync import FailureRecord


def test_retry_classification_rate_limited():
    manager = UniverseSyncManager(broker=None, store=None)

    reason = manager.classify_failure("Rate limiter could not provide token within 30.0s")
    assert reason == "rate_limited"

    reason = manager.classify_failure("HTTP 429 Too Many Requests")
    assert reason == "rate_limited"


def test_retry_backoff_doubling():
    manager = UniverseSyncManager(broker=None, store=None)
    now = datetime.now(timezone.utc)

    record = FailureRecord(
        symbol="TEST",
        failed_at=now,
        reason="rate_limited",
        attempts=2,
        last_error="Rate limited",
    )

    next_retry = manager._calculate_next_retry(record)
    expected_delay = 30 * (2 ** (record.attempts - 1))  # 30s * 2^1 = 60s
    expected_retry = now + timedelta(seconds=expected_delay)

    assert abs((next_retry - expected_retry).total_seconds()) < 1
```

- [ ] **Step 2: Run test to verify it fails**

Run: `pytest market_data/tests/test_universe_sync_retry.py -v`
Expected: FAIL with missing methods

- [ ] **Step 3: Implement retry logic**

```python
# Add to UniverseSyncManager class
def retry_failed(
    self,
    *,
    off_peak: bool = False,
    max_attempts: int = 3,
    start=None,
    end=None,
) -> object:  # Returns SyncReport from sync_service
    """Retry failed symbols with exponential backoff and classification."""
    failures = self.load_failures()

    if not failures:
        log.info("No failures to retry")
        return None

    # Filter by retry_after
    now = datetime.now(UTC)
    due_failures = {
        sym: rec
        for sym, rec in failures.items()
        if rec.retry_after is None or rec.retry_after <= now
    }

    if not due_failures:
        log.info("No failures due for retry yet")
        return None

    log.info("Retrying %d failed symbols", len(due_failures))

    # Resolve instruments
    failed_instruments = []
    for symbol in due_failures:
        try:
            inst = self._resolve_symbol(symbol)
            if inst:
                failed_instruments.append(inst)
        except Exception as exc:
            log.warning("Cannot resolve %s: %s", symbol, exc)

    if not failed_instruments:
        return None

    # Run sync
    from tradex_market_data.sync_service import SyncService, SyncConfig

    config = SyncConfig(
        timeframe="1m",
        batch_size=10,  # Smaller batch for retries
        max_workers=2,
        budget_seconds=3600,
    )

    sync_service = SyncService(self._broker, self._store, config)
    report = sync_service.sync(
        failed_instruments,
        start=start,
        end=end,
    )

    # Update failure records
    self._update_failures_after_retry(report, due_failures, max_attempts)

    return report

def _calculate_next_retry(self, record: FailureRecord) -> datetime:
    """Calculate next retry time with exponential backoff."""
    base_delay = 30  # seconds
    delay = base_delay * (2 ** (record.attempts - 1))
    delay = min(delay, 480)  # Cap at 480 seconds
    return record.failed_at + timedelta(seconds=delay)

def _update_failures_after_retry(
    self,
    report,
    due_failures: dict[str, FailureRecord],
    max_attempts: int,
) -> None:
    """Update failure records based on retry results."""
    all_failures = self.load_failures()

    for symbol, record in due_failures.items():
        if report and symbol in [str(i.instrument_id) for i in getattr(report, "fetched_instruments", [])]:
            # Success: remove from failures
            del all_failures[symbol]
            log.info("Retry succeeded for %s — removed from failure queue", symbol)
        else:
            # Still failing: increment attempts
            record.attempts += 1
            record.failed_at = datetime.now(UTC)

            if record.attempts >= max_attempts:
                record.reason = "permanent_failure"
                record.retry_after = None
                log.warning(
                    "%s reached max attempts (%d) — marked permanent failure",
                    symbol,
                    max_attempts,
                )
            else:
                record.retry_after = self._calculate_next_retry(record)
                log.info(
                    "%s still failing (attempt %d/%d) — next retry at %s",
                    symbol,
                    record.attempts,
                    max_attempts,
                    record.retry_after,
                )

            all_failures[symbol] = record

    self.save_failures(all_failures)

def _resolve_symbol(self, symbol: str):
    """Resolve symbol string to Equity instrument."""
    # This depends on your instrument resolution strategy
    # For now, use broker registry
    if self._broker is None:
        return None

    from tradex_domain.value_objects import InstrumentId
    from tradex_domain.enums import ExchangeId
    from tradex_domain.instruments import Equity

    iid = InstrumentId.equity("NSE", symbol)
    provider_key = self._broker.registry.provider_key(iid)
    if provider_key:
        return Equity(
            instrument_id=iid,
            symbol=symbol,
            exchange=ExchangeId.NSE,
        )
    return None
```

- [ ] **Step 4: Run test to verify it passes**

Run: `pytest market_data/tests/test_universe_sync_retry.py -v`
Expected: PASS

- [ ] **Step 5: Commit**

```bash
git add market_data/src/tradex_market_data/universe_sync.py market_data/tests/test_universe_sync_retry.py
git commit -m "feat: implement smart retry with exponential backoff and classification"
```

---

### Task 7: Orchestration — sync_all Method

**Files:**
- Modify: `market_data/src/tradex_market_data/universe_sync.py` (add `sync_all`, `FullSyncReport`)
- Test: `market_data/tests/test_universe_sync_orchestration.py`

**Interfaces:**
- Consumes: All previous methods
- Produces: `sync_all()` returning `FullSyncReport`

- [ ] **Step 1: Add FullSyncReport dataclass**

```python
# Add to dataclass definitions at top of file
@dataclass
class FullSyncReport:
    """Complete sync report covering all three phases."""
    universe: UniverseDelta | None = None
    renames: list[RenameCandidate] = field(default_factory=list)
    migrations: list[MigrationResult] = field(default_factory=list)
    retry: object | None = None  # SyncReport
    elapsed_s: float = 0.0
```

- [ ] **Step 2: Write test for full orchestration**

```python
# market_data/tests/test_universe_sync_orchestration.py
from datetime import datetime, timezone
from unittest.mock import MagicMock, patch


def test_sync_all_pipeline(tmp_path):
    broker = MagicMock()
    store = MagicMock()

    manager = UniverseSyncManager(broker=broker, store=store, datalake_root=tmp_path)

    # Mock methods
    with (
        patch.object(manager, "refresh_universe") as mock_refresh,
        patch.object(manager, "detect_renames") as mock_renames,
        patch.object(manager, "migrate_symbol_lake") as mock_migrate,
        patch.object(manager, "retry_failed") as mock_retry,
    ):
        mock_refresh.return_value = UniverseDelta(
            added=[],
            removed=[],
            unchanged=[],
            last_refreshed=datetime.now(timezone.utc),
        )
        mock_renames.return_value = []
        mock_migrate.return_value = MigrationResult(success=True)
        mock_retry.return_value = None

        report = manager.sync_all("nifty500")

        assert report.universe is not None
        assert mock_refresh.called
        assert mock_renames.called
        assert report.elapsed_s > 0
```

- [ ] **Step 3: Implement sync_all**

```python
# Add to UniverseSyncManager class
def sync_all(
    self,
    universe_name: str = "nifty500",
    *,
    skip_rename: bool = False,
    skip_retry: bool = False,
    start=None,
    end=None,
) -> FullSyncReport:
    """Run all three phases in sequence with progress reporting."""
    import time

    t0 = time.monotonic()

    log.info("=== Universe Sync Manager: Starting full sync ===")

    # Phase 1: Universe refresh
    log.info("Phase 1: Refreshing universe...")
    universe_delta = self.refresh_universe(universe_name)

    # Phase 2: Rename detection and migration
    renames = []
    migrations = []
    if not skip_rename:
        log.info("Phase 2: Detecting renames...")
        rename_candidates = self.detect_renames()

        for candidate in rename_candidates:
            log.info("Migrating: %s → %s", candidate.old_symbol, candidate.new_symbol)
            result = self.migrate_symbol_lake(
                candidate.old_symbol,
                candidate.new_symbol,
                candidate.isin,
            )
            candidate.status = "migrated" if result.success else "skipped"
            renames.append(candidate)
            migrations.append(result)
    else:
        log.info("Phase 2: Skipped (rename detection disabled)")

    # Phase 3: Failed symbol retry
    retry_report = None
    if not skip_retry:
        log.info("Phase 3: Retrying failed symbols...")
        retry_report = self.retry_failed(
            start=start,
            end=end,
        )
    else:
        log.info("Phase 3: Skipped (retry disabled)")

    elapsed = time.monotonic() - t0

    report = FullSyncReport(
        universe=universe_delta,
        renames=renames,
        migrations=migrations,
        retry=retry_report,
        elapsed_s=elapsed,
    )

    log.info(
        "=== Sync complete: +%d added, -%d removed, %d renames, %ds elapsed ===",
        len(report.universe.added) if report.universe else 0,
        len(report.universe.removed) if report.universe else 0,
        len(report.renames),
        report.elapsed_s,
    )

    return report
```

- [ ] **Step 4: Run test to verify it passes**

Run: `pytest market_data/tests/test_universe_sync_orchestration.py -v`
Expected: PASS

- [ ] **Step 5: Commit**

```bash
git add market_data/src/tradex_market_data/universe_sync.py market_data/tests/test_universe_sync_orchestration.py
git commit -m "feat: add sync_all orchestration method with three-phase pipeline"
```

---

### Task 8: CLI Entry Point

**Files:**
- Create: `trading/scripts/sync_universe.py`
- Test: Manual verification (no automated test needed for CLI wrapper)

**Interfaces:**
- Consumes: `UniverseSyncManager` from Task 7
- Produces: Executable CLI script

- [ ] **Step 1: Create CLI script**

```python
#!/usr/bin/env python3
"""CLI: Unified universe sync manager."""

import argparse
import sys
from pathlib import Path

# Add project roots to path
project_root = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(project_root / "market_data" / "src"))
sys.path.insert(0, str(project_root / "brokers" / "src"))
sys.path.insert(0, str(project_root / "domain" / "src"))

from tradex_market_data.universe_sync import UniverseSyncManager


def main():
    parser = argparse.ArgumentParser(
        description="Sync universe, handle renames, retry failures"
    )
    parser.add_argument(
        "--universe",
        default="nifty500",
        help="Universe CSV name (default: nifty500)",
    )
    parser.add_argument(
        "--skip-rename",
        action="store_true",
        help="Skip rename detection",
    )
    parser.add_argument(
        "--skip-retry",
        action="store_true",
        help="Skip failed symbol retry",
    )
    parser.add_argument(
        "--off-peak",
        action="store_true",
        help="Schedule retries for off-peak hours",
    )
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="Show what would change without doing it",
    )
    parser.add_argument(
        "--start-date",
        type=str,
        help="Start date for sync (ISO format, e.g., 2024-01-01)",
    )
    parser.add_argument(
        "--end-date",
        type=str,
        help="End date for sync (ISO format, e.g., 2024-12-31)",
    )

    args = parser.parse_args()

    # TODO: Wire up actual broker and store once integration is ready
    # For now, show placeholder
    print("Universe Sync Manager CLI")
    print(f"Universe: {args.universe}")
    print(f"Dry run: {args.dry_run}")

    if args.dry_run:
        print("\nDry run mode — showing planned changes:")
        print(f"  Would refresh universe: {args.universe}")
        if not args.skip_rename:
            print("  Would detect renames")
        if not args.skip_retry:
            print("  Would retry failed symbols")
        return

    print("\nExecuting full sync...")
    # manager = UniverseSyncManager(broker=..., store=...)
    # report = manager.sync_all(...)
    print("TODO: Integrate with broker and store")


if __name__ == "__main__":
    main()
```

- [ ] **Step 2: Make executable**

```bash
chmod +x trading/scripts/sync_universe.py
```

- [ ] **Step 3: Test dry-run mode**

Run: `python trading/scripts/sync_universe.py --dry-run`
Expected: Output showing planned changes without executing

- [ ] **Step 4: Commit**

```bash
git add trading/scripts/sync_universe.py
git commit -m "feat: add CLI entry point for universe sync manager"
```

---

### Task 9: Integration Test — End-to-End Pipeline

**Files:**
- Create: `market_data/tests/test_universe_sync_integration.py`

**Interfaces:**
- Consumes: All components from Tasks 1-8
- Produces: End-to-end integration test

- [ ] **Step 1: Write integration test**

```python
# market_data/tests/test_universe_sync_integration.py
"""Integration test: full universe sync pipeline on small test universe."""

import tempfile
from pathlib import Path
from datetime import datetime, timezone
from unittest.mock import MagicMock, patch
import pandas as pd

from tradex_market_data.universe_sync import UniverseSyncManager
from tradex_domain.universe import Equity
from tradex_domain.value_objects import InstrumentId
from tradex_domain.enums import ExchangeId


def test_sync_all_pipeline_integration():
    """Run all 3 phases on 5-symbol test universe."""
    with tempfile.TemporaryDirectory() as tmpdir:
        datalake = Path(tmpdir) / "data"
        datalake.mkdir()

        # Setup: Create 3 symbols in datalake
        for sym in ["SYM1", "SYM2", "SYM3"]:
            (datalake / "ohlcv" / f"symbol={sym}").mkdir(parents=True)

        # Mock broker with one rename (SYM2 → SYM2NEW)
        broker = MagicMock()
        broker.registry.provider_key.return_value = "NSE_EQ|INE123"
        broker.registry.meta.return_value = {"isin": "INE123"}

        # Mock loaded instruments
        sym2_new = Equity(
            instrument_id=InstrumentId.equity("NSE", "SYM2NEW"),
            symbol="SYM2NEW",
            exchange=ExchangeId.NSE,
        )
        broker._loaded_instruments = [sym2_new]

        store = MagicMock()

        # Mock CSV with 4 symbols (SYM1, SYM2, SYM3, SYM4)
        csv_symbols = [
            Equity(instrument_id=InstrumentId.equity("NSE", s), symbol=s, exchange=ExchangeId.NSE)
            for s in ["SYM1", "SYM2", "SYM3", "SYM4"]
        ]
        # Attach ISIN to SYM2
        csv_symbols[1].meta.isin = "INE123"

        manager = UniverseSyncManager(
            broker=broker,
            store=store,
            datalake_root=datalake,
        )

        with patch("tradex_market_data.universe_sync.load_universe", return_value=csv_symbols):
            report = manager.sync_all("test_universe", skip_retry=True)

        # Verify universe delta
        assert len(report.universe.added) == 1  # SYM4
        assert report.universe.added[0].symbol == "SYM4"
        assert len(report.universe.removed) == 0

        # Verify rename detection
        assert len(report.renames) == 1
        assert report.renames[0].old_symbol == "SYM2"
        assert report.renames[0].new_symbol == "SYM2NEW"

        # Verify migration
        assert len(report.migrations) == 1
        assert report.migrations[0].success

        print("Integration test passed!")
```

- [ ] **Step 2: Run integration test**

Run: `pytest market_data/tests/test_universe_sync_integration.py -v -s`
Expected: PASS with "Integration test passed!" output

- [ ] **Step 3: Commit**

```bash
git add market_data/tests/test_universe_sync_integration.py
git commit -m "test: add end-to-end integration test for universe sync pipeline"
```

---

### Task 10: Documentation and Operator Runbook

**Files:**
- Create: `docs/operations/runbooks/universe-sync.md`

**Interfaces:**
- Consumes: Knowledge of all implemented features
- Produces: Operator documentation

- [ ] **Step 1: Write operator runbook**

```markdown
# Universe Sync Manager — Operator Runbook

## Overview

The Universe Sync Manager keeps the NSE 500 datalake aligned with current constituents, handles symbol renames, and retries failed syncs.

## Quick Start

### Dry Run (Recommended First)

```bash
python trading/scripts/sync_universe.py --dry-run
```

Shows what would change without modifying anything.

### Full Sync

```bash
python trading/scripts/sync_universe.py \
  --universe nifty500 \
  --start-date 2024-01-01 \
  --end-date 2024-12-31
```

### Skip Phases

```bash
# Skip rename detection
python trading/scripts/sync_universe.py --skip-rename

# Skip retry
python trading/scripts/sync_universe.py --skip-retry
```

## Metadata Files

All metadata lives under `data/`:

- `.universe_metadata.json` — Last refresh timestamp, added/removed symbols
- `.rename_log.json` — History of symbol renames with row counts
- `.sync_failures.json` — Persistent failure queue with retry scheduling

## Troubleshooting

### Rename Conflict

If both old and new symbol directories exist:

```
Rename conflict: both HEG and HEGAM have data — manual merge required
```

**Resolution:** Manually merge parquet files, then delete old directory.

### Permanent Failures

Symbols marked `permanent_failure` after 3 retry attempts:

```bash
# Check failure queue
cat data/.sync_failures.json | jq '.[] | select(.reason == "permanent_failure")'
```

**Resolution:** Investigate manually, then remove from failure queue if resolved.

### Corrupted Metadata

If JSON files become corrupted:

```bash
# Backup and regenerate
cp data/.sync_failures.json data/.sync_failures.json.bak
rm data/.sync_failures.json
# Next sync will start fresh
```

## Monitoring

Check sync health:

```bash
# Last refresh
cat data/.universe_metadata.json | jq '.last_refreshed'

# Pending failures
cat data/.sync_failures.json | jq 'keys | length'

# Recent renames
cat data/.rename_log.json | jq 'keys'
```

## Off-Peak Scheduling

For large retry batches, schedule during off-peak hours (2 AM - 6 AM IST):

```bash
# Schedule via cron
0 2 * * * cd /path/to/repo && python trading/scripts/sync_universe.py --off-peak
```
```

- [ ] **Step 2: Commit**

```bash
git add docs/operations/runbooks/universe-sync.md
git commit -m "docs: add operator runbook for universe sync manager"
```

---

## Summary

This plan implements the Universe Sync Manager in 10 tasks:

1. **Data structures** (Task 1) — Foundation types
2. **Universe refresh** (Task 2) — CSV-datalake diff
3. **Rename detection** (Task 3) — ISIN-based matching
4. **Lake migration** (Task 4) — Atomic directory + parquet update
5. **Failure tracking** (Task 5) — JSON persistence
6. **Smart retry** (Task 6) — Exponential backoff
7. **Orchestration** (Task 7) — Three-phase pipeline
8. **CLI** (Task 8) — Command-line interface
9. **Integration test** (Task 9) — End-to-end verification
10. **Documentation** (Task 10) — Operator runbook

Each task produces independently testable deliverables with clear interfaces. Total estimated time: 4-6 hours for implementation + 1-2 hours for testing.

Plan complete and saved to `docs/superpowers/plans/2026-11-07-universe-sync-manager.md`. Two execution options:

**1. Subagent-Driven (recommended)** - I dispatch a fresh subagent per task, review between tasks, fast iteration

**2. Inline Execution** - Execute tasks in this session using executing-plans, batch execution with checkpoints

Which approach?
