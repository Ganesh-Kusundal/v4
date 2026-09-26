from pathlib import Path

from tradex_operations import METRICS_CATALOG, list_runbooks, runbook_path


def test_list_runbooks_covers_required_names() -> None:
    names = set(list_runbooks())
    required = {
        "stale-feed",
        "broker-disconnect",
        "unknown-submission",
        "risk-halt",
        "oms-recovery",
        "reconciliation-drift",
        "database-failure",
    }
    assert required <= names


def test_runbook_files_exist() -> None:
    for name in list_runbooks():
        path = runbook_path(name)
        assert path.is_file(), path
        assert path.stat().st_size > 0


def test_metrics_catalog_exists() -> None:
    assert METRICS_CATALOG.is_file()
    assert "feed_age" in METRICS_CATALOG.read_text() or "feed" in METRICS_CATALOG.read_text().lower()
