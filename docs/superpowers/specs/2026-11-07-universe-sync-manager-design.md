# Universe Sync Manager — unified design

Date: 2026-11-07.

## Motivation

The datalake sync service handles bulk historical data well, but three operational gaps cause drift over time:

1. **Symbol renames** — NSE changes trading symbols (e.g., HEG→HEGAM) while keeping the same ISIN; the universe CSV lags behind the broker master, causing `no provider key` failures or stale directory names in the lake.
2. **Universe staleness** — The Nifty 500 constituents change quarterly; the checked-in CSV (`Dependencies/nifty500_list.csv`) gets outdated, so the datalake may contain delisted symbols and miss new additions.
3. **Failed symbol recovery** — Transient failures (rate limits, timeouts) leave symbols incomplete; there's no structured retry mechanism with failure classification and backoff tracking.

This spec designs a unified `UniverseSyncManager` that orchestrates all three concerns in one coordinated workflow, ensuring the datalake stays aligned with the current NSE universe.

## Goals

- **Rename continuity**: Detect when a symbol's trading name changes (same ISIN, different symbol), migrate the lake directory and parquet contents, and log the rename for audit.
- **Universe freshness**: Diff the current Nifty 500 CSV against the datalake, add missing symbols, archive delisted ones, and track metadata about the refresh.
- **Smart retry**: Persist failed sync attempts with reason codes, implement exponential backoff per symbol, and provide off-peak retry scheduling.
- **Single orchestration**: One CLI command (`tradex sync-universe --all`) runs all three phases in sequence with clear progress reporting.
- **Zero data loss**: Lake migrations are atomic and reversible; failures mid-migration leave the lake in a consistent state.

## Non-goals

- Real-time NSE constituent monitoring (this is a batch operation, not a streaming feed).
- Cross-exchange universe management (NSE equity only; BSE/MCX are separate concerns).
- Automated CSV fetching from NSE website (operator updates the CSV manually or via external script).
- Tier 3 ISIN-keyed instrument IDs (trading-symbol keying remains canonical; ISIN is an alias layer).

## Architecture

### New module: `market_data/src/tradex_market_data/universe_sync.py`

```python
class UniverseSyncManager:
    """Unified orchestrator for universe alignment and sync recovery."""
    
    def __init__(
        self,
        broker: Any,
        store: ParquetStorage,
        csv_dir: Path | None = None,
        config: SyncConfig | None = None,
    ) -> None:
        ...
    
    # Phase 1: Universe refresh
    def refresh_universe(
        self,
        universe_name: str = "nifty500",
        csv_path: Path | None = None,
    ) -> UniverseDelta:
        """Diff current universe CSV against datalake and return added/removed symbols."""
        ...
    
    # Phase 2: Rename detection
    def detect_renames(self) -> list[RenameCandidate]:
        """Compare universe ISINs against broker master to find renamed symbols."""
        ...
    
    def migrate_symbol_lake(
        self,
        old_symbol: str,
        new_symbol: str,
        isin: str,
    ) -> MigrationResult:
        """Rename lake directory, update parquet symbol column, log the rename."""
        ...
    
    # Phase 3: Failed symbol retry
    def load_failures(self) -> dict[str, FailureRecord]:
        """Load persistent failure tracker from data/.sync_failures.json."""
        ...
    
    def retry_failed(
        self,
        *,
        off_peak: bool = False,
        max_attempts: int = 3,
    ) -> SyncReport:
        """Retry failed symbols with exponential backoff and classification."""
        ...
    
    # Orchestration
    def sync_all(
        self,
        universe_name: str = "nifty500",
        *,
        skip_rename: bool = False,
        skip_retry: bool = False,
    ) -> FullSyncReport:
        """Run all three phases in sequence with progress reporting."""
        ...
```

### Supporting data structures

```python
@dataclass
class UniverseDelta:
    added: list[Equity]          # In CSV but not in datalake
    removed: list[str]           # In datalake but not in CSV
    unchanged: list[Equity]      # Already synced
    last_refreshed: datetime
    
@dataclass
class RenameCandidate:
    old_symbol: str
    new_symbol: str
    isin: str
    confidence: float            # 1.0 if exact ISIN match, lower if fuzzy
    status: str                  # "pending" | "migrated" | "skipped"

@dataclass
class MigrationResult:
    success: bool
    rows_migrated: int
    error: str | None
    
@dataclass
class FailureRecord:
    symbol: str
    failed_at: datetime
    reason: str                  # "rate_limited" | "not_found" | "timeout" | "delisted"
    attempts: int
    last_error: str
    retry_after: datetime | None
    
@dataclass
class FullSyncReport:
    universe: UniverseDelta
    renames: list[RenameCandidate]
    migrations: list[MigrationResult]
    retry: SyncReport
    elapsed_s: float
```

### Metadata files

All metadata lives under `data/` alongside the lake:

**`data/.universe_metadata.json`**
```json
{
  "universe_name": "nifty500",
  "last_refreshed": "2026-11-07T10:30:00+05:30",
  "source_csv": "Dependencies/nifty500_list.csv",
  "total_symbols": 500,
  "added": ["NEWCO1", "NEWCO2"],
  "removed": ["OLDSTOCK"],
  "checksum": "sha256:abc123..."
}
```

**`data/.rename_log.json`**
```json
{
  "HEG": {
    "new_symbol": "HEGAM",
    "isin": "INE545A01024",
    "renamed_at": "2026-11-07T11:00:00+05:30",
    "reason": "corporate_rename",
    "lake_migrated": true,
    "rows_affected": 45000
  }
}
```

**`data/.sync_failures.json`**
```json
{
  "ABLBL": {
    "symbol": "ABLBL",
    "failed_at": "2026-09-18T13:43:09+05:30",
    "reason": "rate_limited",
    "attempts": 3,
    "last_error": "Rate limiter could not provide a token within 30.0s",
    "retry_after": "2026-11-08T02:00:00+05:30"
  }
}
```

## Implementation details

### Phase 1: Universe Refresh

1. Load current Nifty 500 CSV via `load_universe("nifty500")`.
2. List datalake directories: `data/ohlcv/symbol=*/`.
3. Compute set difference:
   - `added = csv_symbols - datalake_symbols`
   - `removed = datalake_symbols - csv_symbols`
4. For removed symbols:
   - Check broker master: if still active on NSE, keep in lake (may be valid non-Nifty stock).
   - If delisted/not found: move directory to `data/archived/symbol={SYM}/`.
5. Write `data/.universe_metadata.json` with delta summary.

### Phase 2: Rename Detection

1. Build ISIN index from broker master after `load_instruments()`:
   ```python
   isin_index = {}
   for inst in loaded_instruments:
       isin = registry.meta(inst.instrument_id).get("isin")
       if isin:
           isin_index[isin] = inst.symbol
   ```
2. For each symbol in universe CSV:
   - Get its ISIN from CSV row.
   - Look up ISIN in broker master index.
   - If broker symbol ≠ CSV symbol: candidate rename detected.
3. Verify candidate:
   - Check if old symbol exists in datalake.
   - Check if new symbol already has data (skip if yes — merge conflict).
4. Return list of `RenameCandidate` objects.

### Phase 3: Lake Migration

For each confirmed rename:

1. **Directory rename**:
   ```bash
   mv data/ohlcv/symbol=HEG data/ohlcv/symbol=HEGAM
   ```
2. **Parquet column update** (for each file):
   ```python
   df = pd.read_parquet(filepath)
   df["symbol"] = new_symbol
   df.to_parquet(filepath, index=False)
   ```
3. **Atomicity**: 
   - Copy directory first, verify row counts match.
   - Only delete old directory after successful copy.
   - Wrap in try/except; rollback on failure.
4. **Logging**: Append entry to `data/.rename_log.json`.

### Phase 4: Failed Symbol Retry

1. Load `data/.sync_failures.json`.
2. Filter by `retry_after <= now()` (or force with `--force`).
3. Classify failures:
   - `"rate_limited"` → retry with longer backoff (2x previous).
   - `"not_found"` → check broker master; if absent, mark `"delisted"`.
   - `"timeout"` → retry with smaller batch size.
   - `"delisted"` → skip permanently, move to quarantine.
4. Run sync for failed symbols only:
   ```python
   failed_instruments = [resolve(symbol) for symbol in failures]
   report = sync_service.sync(failed_instruments, start, end)
   ```
5. Update failure records:
   - Success: remove from `.sync_failures.json`.
   - Still failing: increment `attempts`, set new `retry_after`.
   - Max attempts reached: mark `"permanent_failure"`.

### CLI Entry Point

**`trading/scripts/sync_universe.py`**

```python
#!/usr/bin/env python3
"""CLI: Unified universe sync manager."""

import argparse
from tradex_market_data.universe_sync import UniverseSyncManager
from tradex_market_data.broker_factory import build_broker_from_env
from tradex_market_data.parquet_storage import ParquetStorage

def main():
    parser = argparse.ArgumentParser(description="Sync universe, handle renames, retry failures")
    parser.add_argument("--universe", default="nifty500", help="Universe CSV name")
    parser.add_argument("--skip-rename", action="store_true", help="Skip rename detection")
    parser.add_argument("--skip-retry", action="store_true", help="Skip failed symbol retry")
    parser.add_argument("--off-peak", action="store_true", help="Schedule retries for off-peak hours")
    parser.add_argument("--dry-run", action="store_true", help="Show what would change without doing it")
    args = parser.parse_args()
    
    broker = build_broker_from_env()
    store = ParquetStorage()
    manager = UniverseSyncManager(broker, store)
    
    if args.dry_run:
        delta = manager.refresh_universe(args.universe)
        print(f"Would add: {len(delta.added)} symbols")
        print(f"Would remove: {len(delta.removed)} symbols")
        if not args.skip_rename:
            renames = manager.detect_renames()
            print(f"Would rename: {len(renames)} symbols")
        return
    
    report = manager.sync_all(
        universe_name=args.universe,
        skip_rename=args.skip_rename,
        skip_retry=args.skip_retry,
    )
    
    print(f"Universe: +{len(report.universe.added)} -{len(report.universe.removed)}")
    print(f"Renames: {len(report.renames)} migrated")
    print(f"Retry: {report.retry.fetched}/{report.retry.requested} fetched")
    print(f"Elapsed: {report.elapsed_s:.0f}s")

if __name__ == "__main__":
    main()
```

## Error handling

| Scenario | Behavior |
|----------|----------|
| Broker API down during rename check | Abort phase 2, log warning, continue to phase 3 if enabled |
| Lake migration fails mid-copy | Rollback: delete partial copy, restore old directory name |
| Universe CSV not found | Raise `FileNotFoundError` with helpful message about manual update |
| Failure tracker corrupted JSON | Log error, start fresh tracker (do not block sync) |
| Rename conflict (both old and new have data) | Skip migration, log manual intervention required |

## Testing

### Unit tests

- `test_detect_rename_same_isin_different_symbol()` — mock broker master with HEG→HEGAM
- `test_universe_delta_added_removed()` — mock CSV with 2 new, 1 removed symbol
- `test_retry_classification_rate_limited()` — verify backoff doubling
- `test_migration_atomic_rollback()` — simulate disk full during copy

### Integration tests

- `test_sync_all_pipeline()` — run all 3 phases on 5-symbol test universe
- `test_rename_lake_continuity()` — verify parquet queries work after migration
- `test_failure_persistence()` — write failures, reload, verify retry logic

### Manual verification

1. Run `python trading/scripts/sync_universe.py --dry-run` to see planned changes.
2. Check generated metadata files for correctness.
3. Verify lake directory structure after migration.
4. Query datalake for renamed symbol: should return continuous history.

## File map

| File | Role |
|------|------|
| `market_data/src/tradex_market_data/universe_sync.py` | Main manager class |
| `trading/scripts/sync_universe.py` | CLI entry point |
| `market_data/tests/test_universe_sync.py` | Unit + integration tests |
| `data/.universe_metadata.json` | Universe refresh tracking (generated) |
| `data/.rename_log.json` | Rename history (generated) |
| `data/.sync_failures.json` | Persistent failure queue (generated) |

## Migration plan

1. Implement `UniverseSyncManager` class with all methods.
2. Add CLI script `sync_universe.py`.
3. Write unit tests for each phase.
4. Run dry-run on production datalake to verify detection accuracy.
5. Execute first full sync with `--skip-retry` to test universe refresh + renames.
6. Enable retry phase and monitor off-peak execution.
7. Document operator workflow in `docs/operations/runbooks/universe-sync.md`.

## Open questions

1. Should archived (delisted) symbols be compressed (`.tar.gz`) or just moved?
2. Do we want a webhook/notification when renames are detected?
3. Should the manager auto-update the CSV from NSE website, or keep it manual?
