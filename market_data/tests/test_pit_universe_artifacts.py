"""The checked-in universe artifacts: membership dates and the delisting ledger.

The universe CSVs are a single undated snapshot, which is the root of the
survivorship bias AC-4 names: a backtest over any past window runs on today's
membership, so names that were dropped from the index before the window are
simply absent. The membership column records *when the snapshot was taken*,
which is the strongest claim the repository can support — nothing here holds
per-name index entry or exit dates, and no configured broker will serve them
for past dates.

The delisting ledger is the exit-side counterpart and is a real, sourced
artifact: it is a transcription of NSE's own published "List of delisted
Companies" file, which the spec's claim that "a vendor does not serve it for
past dates" got wrong. The tests below pin the transcription's shape, its
agreement with ``load_delistings`` and the universe files, and the two
properties that make it usable as a survivorship record at all.

Tests live here because ``tradex_domain.universe`` reads these files and
``tradex_market_data`` is the package whose backtest loader consumes them.
"""

from __future__ import annotations

import csv
from datetime import date
from pathlib import Path

import pytest
from tradex_domain.universe import available_universes, load_universe
from tradex_research.pit import load_delistings

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

#: NSE's published file, and what the transcription must reproduce.
#: https://nsearchives.nseindia.com/web/sites/default/files/inline-files/List%20of%20delisted%20Companies.xlsx
#: sheet ``delisted`` — 438 data rows, 2002-04-15 … 2024-10-17.
_DELISTINGS_ROWS = 438
_DELISTINGS_SPAN = ("2002-04-15", "2024-10-17")


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


def _delistings_rows() -> list[dict[str, str]]:
    with open(_DEPENDENCIES / "nse_delistings.csv", newline="", encoding="utf-8") as fh:
        return list(csv.DictReader(fh))


def _shipped_ledger() -> dict[str, date | None]:
    """The worktree's own ledger, read through the guard's own reader.

    ``load_delistings()`` with no path anchors to the ``tradex_research``
    *package's* checkout, which in a worktree is not necessarily the one under
    test. Everything here is about ``_DEPENDENCIES``, so the path is passed
    explicitly and the artifact measured is the artifact asserted on.
    """
    return load_delistings(_DEPENDENCIES / "nse_delistings.csv")


def test_delistings_artifact_carries_every_sourced_row() -> None:
    """Row count and date span are pinned to NSE's published file.

    The number is a transcription target, not a constant that happens to
    match: a maintainer re-downloading the source and finding a different
    count has learned that NSE published a newer file, which is a reason to
    re-transcribe, not a reason to fudge the test.
    """
    rows = _delistings_rows()
    assert len(rows) == _DELISTINGS_ROWS
    dates = sorted(row["delist_date"] for row in rows)
    assert (dates[0], dates[-1]) == _DELISTINGS_SPAN


def test_every_delisting_date_is_a_real_iso_date() -> None:
    """A date nobody can parse is not a date, and a guessed one rewrites
    every backtest window that spans it."""
    for row in _delistings_rows():
        assert date.fromisoformat(row["delist_date"])


def test_delisted_symbols_match_nse_spelling_and_are_never_ambiguous() -> None:
    """Symbols are upper-case, and every one of them is something NSE lists.

    A row whose symbol also appears in a universe file would be a conflict the
    reader cannot resolve — the same trading symbol both a current member and
    a delisted name — so the intersection is asserted to be empty rather than
    left for whoever reads the file to notice. (It is empty: NSE's file
    contains no current Nifty 500 name, by symbol or by ISIN.)
    """
    rows = _delistings_rows()
    symbols = {row["symbol"] for row in rows}
    for row in rows:
        assert row["symbol"] == row["symbol"].strip().upper()
    universe_symbols: set[str] = set()
    for name in _EXPECTED_MEMBERS:
        with open(_DEPENDENCIES / f"{name}_list.csv", newline="", encoding="utf-8") as fh:
            universe_symbols |= {r["Symbol"].strip().upper() for r in csv.DictReader(fh)}
    assert not (symbols & universe_symbols), sorted(symbols & universe_symbols)


def test_a_duplicate_symbol_keeps_both_sourced_dates() -> None:
    """``load_delistings`` keys on symbol, so two rows collapse to one entry.

    NSE's own file lists ``VIKASHMET`` twice under a single ISIN with two
    delisting dates. The ledger keeps both rows — the source says both, and
    dropping one would be inventing a single history the exchange never
    claimed — and the loader's documented last-wins collapse is what the next
    test pins.
    """
    vikash = [row for row in _delistings_rows() if row["symbol"] == "VIKASHMET"]
    assert len(vikash) == 2
    ledger = _shipped_ledger()
    assert len(ledger) == len(_delistings_rows()) - 1
    assert ledger["VIKASHMET"] == date.fromisoformat(
        max(row["delist_date"] for row in vikash)
    )


def test_the_ledger_loads_and_is_not_empty() -> None:
    """The exit half of the guard is only as good as this file, and it is now
    a real one: an empty ledger established nothing and every load said so."""
    ledger = _shipped_ledger()
    assert ledger
    assert all(d is not None for d in ledger.values())
    dated = [d for d in ledger.values() if d is not None]
    assert min(dated) == date.fromisoformat(_DELISTINGS_SPAN[0])
    assert max(dated) == date.fromisoformat(_DELISTINGS_SPAN[1])
