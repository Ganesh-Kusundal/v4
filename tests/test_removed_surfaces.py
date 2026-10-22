"""Deleted packages must stay deleted.

Every path here was removed because it had no production caller. The guard
fails if one comes back, and it is the single place to add the next deletion.
"""

from __future__ import annotations

from pathlib import Path

_ROOT = Path(__file__).resolve().parent.parent


def test_removed_trees_stay_removed() -> None:
    removed = [
        "research",
        "trading/tests/research",
        "market_data/tests/test_pit_universe_artifacts.py",
    ]
    assert [path for path in removed if (_ROOT / path).exists()] == []


def test_pyproject_names_no_removed_member() -> None:
    text = (_ROOT / "pyproject.toml").read_text(encoding="utf-8")
    for needle in ("research/tests", "research/src", "tradex-research", "tradex_research"):
        assert needle not in text, f"pyproject still names {needle}"
