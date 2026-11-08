"""Universe sync manager — unified orchestrator for universe alignment."""

from __future__ import annotations

import json
import logging
import shutil
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pandas as pd

from tradex_domain.instruments import Equity
from tradex_domain.universe import load_universe

log = logging.getLogger(__name__)


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

    def detect_renames(
        self,
        universe_instruments: list[Equity] | None = None,
    ) -> list[RenameCandidate]:
        """Compare universe ISINs against broker master to find renamed symbols."""
        if self._broker is None:
            log.warning("No broker available for rename detection")
            return []

        if universe_instruments is None:
            # Use existing _universe_instruments if set
            universe_instruments = getattr(self, "_universe_instruments", None)  # type: ignore[attr-defined]
            if universe_instruments is None:
                # Fallback: load from default universe
                try:
                    universe_instruments = load_universe("nifty500", csv_dir=self._csv_dir)
                except FileNotFoundError:
                    return []

        self._universe_instruments = universe_instruments  # type: ignore[attr-defined]

        # Build ISIN index from broker master
        isin_index = self._build_isin_index()

        candidates = []
        for inst in universe_instruments:
            csv_isin = getattr(getattr(inst, "meta", None), "isin", None)
            if not csv_isin:
                continue

            broker_symbol = isin_index.get(str(csv_isin).strip().upper())
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
        isin_index: dict[str, str] = {}
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

    def retry_failed(
        self,
        *,
        off_peak: bool = False,
        max_attempts: int = 3,
        start=None,
        end=None,
    ) -> object | None:  # Returns SyncReport from sync_service
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
