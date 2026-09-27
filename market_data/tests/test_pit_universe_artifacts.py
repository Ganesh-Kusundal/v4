"""The checked-in universe artifacts: membership dates and the delisting ledger.

The universe CSVs are a single undated snapshot, which is the root of the
survivorship bias AC-4 names: a backtest over any past window runs on today's
membership, so names that were dropped from the index before the window are
simply absent. The membership column records *when the snapshot was taken*,
which is the strongest claim the repository can support — nothing here holds
per-name index entry or exit dates, and no configured broker will serve them
for past dates.

Tests live here because ``tradex_domain.universe`` reads these files and
``tradex_market_data`` is the package whose backtest loader consumes them.
"""

from __future__ import annotations

import csv
from datetime import date
from pathlib import Path

import pytest
from tradex_domain.universe import available_universes, load_universe

_DEPENDENCIES = Path(__file__).resolve().parents[2] / "Dependencies"

_MEMBERSHIP_COLUMN = "membership_asof"

#: The date each snapshot was last refreshed, taken from the git history of the
#: file itself (``git log -1 --date=short -- <csv>``). A file mtime is not a
#: provenance record — a fresh clone resets it — so the commit date is the only
#: date about these snapshots that a reviewer can reproduce.
_SNAPSHOT_ASOF = {
    "nifty50": "2026-08-12",
    "nifty100": "2026-08-12",
    "nifty200": "2026-08-12",
    "nifty500": "2026-09-22",
}

_EXPECTED_MEMBERS = {"nifty50": 50, "nifty100": 100, "nifty200": 200, "nifty500": 500}

_DELISTINGS_HEADER = ["symbol", "delist_date", "series"]


def _rows(name: str) -> list[dict[str, str]]:
    with open(_DEPENDENCIES / f"{name}_list.csv", newline="", encoding="utf-8") as fh:
        return list(csv.DictReader(fh))


def test_available_universes_is_unchanged() -> None:
    """The delisting ledger must not be offered as a universe.

    ``available_universes`` globs ``nifty*_list.csv``, so a new CSV in
    ``Dependencies/`` is only invisible if the glob stays as narrow as it is.
    """
    assert available_universes() == ["nifty100", "nifty200", "nifty50", "nifty500"]


@pytest.mark.parametrize("name", sorted(_SNAPSHOT_ASOF))
def test_membership_date_column_is_present_and_pinned(name: str) -> None:
    rows = _rows(name)
    assert rows, f"{name}_list.csv has no data rows"
    for row in rows:
        assert _MEMBERSHIP_COLUMN in row, f"{name}_list.csv has no {_MEMBERSHIP_COLUMN} column"
        assert row[_MEMBERSHIP_COLUMN] == _SNAPSHOT_ASOF[name], (
            f"{name}: {row.get('Symbol')} claims membership as of "
            f"{row[_MEMBERSHIP_COLUMN]!r}, expected {_SNAPSHOT_ASOF[name]}"
        )


@pytest.mark.parametrize("name", sorted(_SNAPSHOT_ASOF))
def test_membership_dates_are_real_iso_dates(name: str) -> None:
    """A column of free text is not a date, and a date nobody parsed is not a guard."""
    for row in _rows(name):
        assert date.fromisoformat(row[_MEMBERSHIP_COLUMN])


@pytest.mark.parametrize("name", sorted(_SNAPSHOT_ASOF))
def test_existing_columns_survive_the_new_one(name: str) -> None:
    """The added column is additive: every column the loader reads is still there."""
    for row in _rows(name):
        for column in ("Company Name", "Industry", "Symbol", "Series", "ISIN Code"):
            assert row[column].strip(), f"{name}: {column} is empty on {row.get('Symbol')}"


@pytest.mark.parametrize("name", sorted(_EXPECTED_MEMBERS))
def test_load_universe_still_returns_the_same_members(name: str) -> None:
    """Regression guard for the extra column.

    ``load_universe`` is a ``csv.DictReader``, so a column it does not name is
    ignored — this is the test that proves it rather than assuming it, and it
    would catch a parser that started rejecting unknown columns.
    """
    instruments = load_universe(name)
    assert len(instruments) == _EXPECTED_MEMBERS[name]
    assert all(inst.symbol for inst in instruments)


def test_delistings_header_is_exactly_as_specified() -> None:
    with open(_DEPENDENCIES / "nse_delistings.csv", newline="", encoding="utf-8") as fh:
        reader = csv.reader(fh)
        header = next(reader)
    assert header == _DELISTINGS_HEADER


def test_delistings_artifact_has_no_rows() -> None:
    """Zero sourced delistings, stated as a number so it cannot drift unnoticed.

    An empty artifact is the honest state: no historical index membership exists
    anywhere in this repository and no broker serves it for past dates, so any
    date written here would be invented — and an invented delisting date silently
    changes every backtest window that spans it. A reviewer adding sourced rows
    must change this test, which makes the provenance of each row a review
    question instead of a detail.
    """
    with open(_DEPENDENCIES / "nse_delistings.csv", newline="", encoding="utf-8") as fh:
        rows = list(csv.DictReader(fh))
    assert rows == [], f"unsourced delisting rows present: {rows}"
