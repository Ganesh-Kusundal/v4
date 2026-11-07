"""Universe sync manager — unified orchestrator for universe alignment."""

from __future__ import annotations

import json
import logging
import shutil
from collections.abc import Iterable
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pandas as pd
from tradex_domain.universe import load_universe
from tradex_domain.instruments import Equity

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
