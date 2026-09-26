"""TradeX operations — runbook and metrics catalog index.

Observes and documents; does not change trading decisions.
"""

from __future__ import annotations

from pathlib import Path

#: Repo-relative docs root for operator runbooks (resolved from this package).
_DOCS_OPERATIONS = Path(__file__).resolve().parents[3] / "docs" / "operations"

RUNBOOKS: dict[str, Path] = {
    "stale-feed": _DOCS_OPERATIONS / "runbooks" / "stale-feed.md",
    "broker-disconnect": _DOCS_OPERATIONS / "runbooks" / "broker-disconnect.md",
    "unknown-submission": _DOCS_OPERATIONS / "runbooks" / "unknown-submission.md",
    "risk-halt": _DOCS_OPERATIONS / "runbooks" / "risk-halt.md",
    "oms-recovery": _DOCS_OPERATIONS / "runbooks" / "oms-recovery.md",
    "reconciliation-drift": _DOCS_OPERATIONS / "runbooks" / "reconciliation-drift.md",
    "database-failure": _DOCS_OPERATIONS / "runbooks" / "database-failure.md",
}

METRICS_CATALOG = _DOCS_OPERATIONS / "metrics-catalog.md"


def runbook_path(name: str) -> Path:
    """Return the path for a named runbook; raise KeyError if unknown."""
    return RUNBOOKS[name]


def list_runbooks() -> tuple[str, ...]:
    """Sorted runbook names."""
    return tuple(sorted(RUNBOOKS))


__all__ = [
    "METRICS_CATALOG",
    "RUNBOOKS",
    "list_runbooks",
    "runbook_path",
]
