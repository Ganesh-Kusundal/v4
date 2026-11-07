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
