"""The point-in-time universe guard inside ``ParquetBacktestLoader.load`` (AC-4).

The guard lives in the loader rather than in a caller because the loader is what
every backtest surface actually calls: the CLI, ``session.backtest.load()`` and
``run_with_loader``. One call site means a third caller cannot forget it.

Two different things are under test here, and conflating them is how a vacuous
pass reads as a clean result:

* the guard **refuses** a run the artifacts already rule out — a symbol the
  declared universe does not hold (entry side), a symbol delisted inside the
  window (exit side) — before a single bar is read; and
* it **reports how far the check got**. The shipped ledger
  (``Dependencies/nse_delistings.csv``) carries the 438 rows NSE publishes in
  its own "List of delisted Companies" file, so the exit side is genuinely
  checked, and each load has to say which.

The nine "acknowledge a limit" tests are kept: they pin the *reporting*
behaviour that a non-vacuous guard still has to get right, and the
``TestTheEntryHalfIsLoadBearing`` class below is what proves the assertion
itself can fail.
"""

from __future__ import annotations

import csv
import logging
from datetime import date, datetime
from pathlib import Path

import pandas as pd
import pytest
from tradex_domain.instruments import Equity
from tradex_market_data import backtest_loader as loader_mod
from tradex_market_data.backtest_loader import ParquetBacktestLoader
from tradex_market_data.parquet_storage import ParquetStorage
from tradex_research.pit import PitViolation, load_delistings

_WINDOW = (datetime(2026, 7, 1), datetime(2026, 7, 31))

#: 2026-07-01 opens 42 days before the nifty50 snapshot's ``membership_asof``.
_NIFTY50_SNAPSHOT = date(2026, 8, 12)

#: The shipped ledger, read as rows rather than as ``load_delistings()``'s dict.
#: The two differ on purpose: the dict is keyed by symbol, and NSE's own file
#: lists ``VIKASHMET`` twice under one ISIN, so 438 rows are 437 symbols.
_LEDGER = Path(loader_mod.DELISTINGS_LEDGER)

#: A symbol no universe file lists — the look-ahead the entry half exists to
#: catch. Named so a violation message in a failing test explains itself.
_OUTSIDER = "NOTININDEX"


def _nifty50_symbols() -> set[str]:
    """Every symbol the nifty50 CSV lists, read straight from the file.

    The entry-half tests stub ``load_universe`` (the loader's *selection*
    source) and rely on ``universe_snapshot`` (the guard's *record* source)
    still reading the file. That split is the property under test, so these
    tests must not accidentally stub both.
    """
    with open(loader_mod.DEPENDENCIES_DIR / "nifty50_list.csv", newline="", encoding="utf-8") as fh:
        return {r["Symbol"].strip().upper() for r in csv.DictReader(fh)}


_NIFTY50_SYMBOLS = _nifty50_symbols()


def _upsert_bars(store: ParquetStorage, symbol: str, days: tuple[int, ...]) -> None:
    """Two 1m bars per weekday of July 2026, the window this file pins."""
    rows = [
        dict(
            symbol=symbol,
            exchange="NSE",
            kind="equity",
            timeframe="1m",
            timestamp=f"2026-07-{day:02d} 09:{minute:02d}:00",
            open=100,
            high=102,
            low=99,
            close=101,
            volume=1000,
        )
        for day in days
        for minute in (15, 16)
    ]
    store.upsert(pd.DataFrame(rows))


def _expected_timestamps() -> list[datetime]:
    return [
        datetime(2026, 7, day, 9, minute)
        for day in (1, 2, 3, 6)  # Wed, Thu, Fri, Mon — 2026-07-04/05 are a weekend
        for minute in (15, 16)
    ]


def _no_read(*args, **kwargs):
    raise AssertionError("a bar was read before the universe was checked")


class TestLoaderRefusesAWindowTheLedgerRulesOut:
    """The negative control: a delisted name inside the window stops the load."""

    def test_a_delisting_inside_the_window_refuses_the_load(self, tmp_path, monkeypatch) -> None:
        store = ParquetStorage(tmp_path)
        _upsert_bars(store, "RELIANCE", (1, 2, 3, 6))
        loader = ParquetBacktestLoader(store=store)
        monkeypatch.setattr(
            loader_mod, "load_delistings", lambda *a, **k: {"RELIANCE": date(2026, 7, 15)}
        )

        with pytest.raises(PitViolation) as excinfo:
            loader.load(
                instruments=[Equity.of("NSE", "RELIANCE")], start=_WINDOW[0], end=_WINDOW[1]
            )
        message = str(excinfo.value)
        assert "RELIANCE" in message
        assert "2026-07-15" in message
        assert "2026-07-01" in message and "2026-07-31" in message

    def test_the_refusal_comes_before_any_bar_is_read(self, tmp_path, monkeypatch) -> None:
        """The guard is a precondition of the load, not a report on its results.

        ``_load_one`` is the only path to the store, so replacing it with a
        raiser fails the test if the check ever runs after the data is in hand.
        """
        store = ParquetStorage(tmp_path)
        _upsert_bars(store, "RELIANCE", (1, 2, 3, 6))
        loader = ParquetBacktestLoader(store=store)
        monkeypatch.setattr(
            loader_mod, "load_delistings", lambda *a, **k: {"RELIANCE": date(2026, 7, 15)}
        )
        monkeypatch.setattr(loader, "_load_one", _no_read)

        with pytest.raises(PitViolation):
            loader.load(
                instruments=[Equity.of("NSE", "RELIANCE")], start=_WINDOW[0], end=_WINDOW[1]
            )

    def test_the_store_symbol_path_is_guarded_too(self, tmp_path, monkeypatch) -> None:
        """Neither selection nor universe is a way round it: no selection means
        every symbol in the store, and that set is checked like any other."""
        store = ParquetStorage(tmp_path)
        _upsert_bars(store, "RELIANCE", (1, 2, 3, 6))
        loader = ParquetBacktestLoader(store=store)
        monkeypatch.setattr(
            loader_mod, "load_delistings", lambda *a, **k: {"RELIANCE": date(2026, 7, 15)}
        )

        with pytest.raises(PitViolation, match="RELIANCE"):
            loader.load(start=_WINDOW[0], end=_WINDOW[1])

    def test_a_delisted_name_the_run_does_not_use_cannot_fail_its_window(
        self, tmp_path, monkeypatch, caplog
    ) -> None:
        """The check is about the symbols the run trades.

        ``delisted=`` drops a name from the universe and store paths, so a
        delisting that no longer matters must not refuse the run — otherwise the
        obvious workaround is to pass a flag, and then to pass a wrong one.
        """
        store = ParquetStorage(tmp_path)
        _upsert_bars(store, "RELIANCE", (1, 2, 3, 6))
        _upsert_bars(store, "TCS", (1, 2, 3, 6))
        loader = ParquetBacktestLoader(store=store, delisted={"TCS"})
        monkeypatch.setattr(
            loader_mod, "load_delistings", lambda *a, **k: {"TCS": date(2026, 7, 15)}
        )

        with caplog.at_level(logging.WARNING):
            candles = loader.load(start=_WINDOW[0], end=_WINDOW[1])
        assert [c.timestamp for c in candles] == _expected_timestamps()
        # RELIANCE is the only member of the run, so it is the only member the
        # report counts — TCS was dropped before the check.
        assert "1 of 1 members used" in caplog.text


class TestLoaderReportsWhatTheCheckCouldNotEstablish:
    def test_a_valid_universe_loads_unchanged(self, tmp_path, caplog) -> None:
        """The guard must not change the outcome of a sound run."""
        store = ParquetStorage(tmp_path)
        _upsert_bars(store, "RELIANCE", (1, 2, 3, 6))
        loader = ParquetBacktestLoader(store=store)

        with caplog.at_level(logging.WARNING):
            candles = loader.load(
                instruments=[Equity.of("NSE", "RELIANCE")], start=_WINDOW[0], end=_WINDOW[1]
            )
        assert [c.timestamp for c in candles] == _expected_timestamps()

    def test_the_sourced_ledger_is_reported_as_checked(self, tmp_path, caplog) -> None:
        """The exit side is no longer unverified, and the report must say so.

        The empty-ledger branch is still reachable — the next test pins it,
        against a monkeypatched ledger — but against the *shipped* file the run
        has 437 sourced delistings behind it, and a report still saying
        "UNVERIFIED - holds 0 delistings" would be understating its own
        evidence.
        """
        store = ParquetStorage(tmp_path)
        _upsert_bars(store, "RELIANCE", (1, 2, 3, 6))
        loader = ParquetBacktestLoader(store=store)

        with caplog.at_level(logging.WARNING):
            loader.load(
                instruments=[Equity.of("NSE", "RELIANCE")], start=_WINDOW[0], end=_WINDOW[1]
            )
        assert "PIT universe check:" in caplog.text
        assert (
            f"checked against {len(load_delistings(_LEDGER))} sourced delisting(s)"
            in caplog.text
        )
        assert "nse_delistings.csv" in caplog.text
        assert "holds 0 delistings" not in caplog.text
        assert "UNVERIFIED" not in caplog.text

    def test_an_empty_ledger_is_still_reported_as_unverified(
        self, tmp_path, monkeypatch, caplog
    ) -> None:
        """The honesty branch, kept by monkeypatching the ledger back to empty.

        The shipped file is sourced, so this cannot be reached through the real
        artifact any more — but the report must keep failing *loudly* rather
        than silently claiming a clean result if the ledger is ever emptied, so
        the branch is pinned here instead of being deleted along with its only
        way in. Each fragment is asserted separately: ``UNVERIFIED`` alone
        could be any line, and a bare number would pass without naming the
        artifact it measured.
        """
        store = ParquetStorage(tmp_path)
        _upsert_bars(store, "RELIANCE", (1, 2, 3, 6))
        loader = ParquetBacktestLoader(store=store)
        monkeypatch.setattr(loader_mod, "load_delistings", lambda *a, **k: {})

        with caplog.at_level(logging.WARNING):
            loader.load(
                instruments=[Equity.of("NSE", "RELIANCE")], start=_WINDOW[0], end=_WINDOW[1]
            )
        assert "exit side:  UNVERIFIED" in caplog.text
        assert "holds 0 delistings" in caplog.text
        assert "nse_delistings.csv" in caplog.text
        assert "this is not a clean result" in caplog.text

    def test_the_reported_count_is_the_shipped_ledger_row_count(
        self, tmp_path, caplog
    ) -> None:
        """The number is a measurement of the artifact, not a constant.

        Compared against the *same file the guard reads* — the loader passes
        ``DELISTINGS_LEDGER`` explicitly rather than relying on
        ``load_delistings()``'s default, which resolves relative to whatever
        checkout ``tradex_research`` was imported from. A test that called the
        bare default here would measure a different artifact than the one the
        run reported on, and would go quietly stale in a worktree.
        """
        store = ParquetStorage(tmp_path)
        _upsert_bars(store, "RELIANCE", (1, 2, 3, 6))
        loader = ParquetBacktestLoader(store=store)

        with caplog.at_level(logging.WARNING):
            loader.load(
                instruments=[Equity.of("NSE", "RELIANCE")], start=_WINDOW[0], end=_WINDOW[1]
            )
        assert (
            f"checked against {len(load_delistings(_LEDGER))} sourced delisting(s)"
            in caplog.text
        )

    def test_a_sourced_delisting_outside_the_window_stops_the_zero_claim(
        self, tmp_path, monkeypatch, caplog
    ) -> None:
        """The non-vacuity control, at a size the shipped file can produce.

        With one sourced delisting dated outside the window the report has to
        count it and drop the empty-ledger sentence. If either were hardcoded
        prose, this fails — which is the whole point of writing it.
        """
        store = ParquetStorage(tmp_path)
        _upsert_bars(store, "RELIANCE", (1, 2, 3, 6))
        loader = ParquetBacktestLoader(store=store)
        monkeypatch.setattr(
            loader_mod, "load_delistings", lambda *a, **k: {"GONESY": date(2027, 1, 1)}
        )

        with caplog.at_level(logging.WARNING):
            loader.load(
                instruments=[Equity.of("NSE", "RELIANCE")], start=_WINDOW[0], end=_WINDOW[1]
            )
        assert "checked against 1 sourced delisting(s)" in caplog.text
        assert "0 delistings" not in caplog.text
        assert "UNVERIFIED" not in caplog.text

    def test_caller_named_symbols_report_no_index_snapshot(self, tmp_path, caplog) -> None:
        """A caller naming its own symbols makes no index-membership claim, and
        the report says that instead of implying the entry side was checked."""
        store = ParquetStorage(tmp_path)
        _upsert_bars(store, "RELIANCE", (1, 2, 3, 6))
        loader = ParquetBacktestLoader(store=store)

        with caplog.at_level(logging.WARNING):
            loader.load(
                instruments=[Equity.of("NSE", "RELIANCE")], start=_WINDOW[0], end=_WINDOW[1]
            )
        assert "caller-named symbols (no index snapshot)" in caplog.text
        assert "entry side: no index snapshot" in caplog.text

    def test_a_universe_load_dates_the_entry_side_against_its_snapshot(
        self, tmp_path, caplog
    ) -> None:
        """A window opening before ``membership_asof`` is trading a composition
        that provably was not the index's, so the report has to say so.

        The members come from the same ``load_universe`` the loader reads, and
        the date from the CSV's own ``membership_asof`` column, so the guard is
        checking the set the load will actually use.
        """
        loader = ParquetBacktestLoader(store=ParquetStorage(tmp_path))

        with caplog.at_level(logging.WARNING):
            loader.load(universe="nifty50", start=_WINDOW[0], end=_WINDOW[1])
        assert f"nifty50 snapshot as-of {_NIFTY50_SNAPSHOT.isoformat()}" in caplog.text
        assert "50 of 50 members used" in caplog.text
        assert "entry side: UNVERIFIED - the window opens 42d before the snapshot date" in (
            caplog.text
        )

    def test_the_window_opening_on_the_snapshot_is_reported_as_checked(
        self, tmp_path, caplog
    ) -> None:
        loader = ParquetBacktestLoader(store=ParquetStorage(tmp_path))

        with caplog.at_level(logging.WARNING):
            loader.load(
                universe="nifty50",
                start=datetime(2026, 8, 20),
                end=datetime(2026, 9, 20),
            )
        assert "entry side: checked - the window opens on or after the snapshot date" in (
            caplog.text
        )


class TestUnboundedLoadDerivesTheWindowItCovered:
    def test_an_unbounded_load_checks_the_window_it_actually_covered(
        self, tmp_path, monkeypatch
    ) -> None:
        """No bound means each symbol's own stored range, which is only known
        once the bars are in hand — so the window is derived from them and the
        delisting inside that span still refuses the run."""
        store = ParquetStorage(tmp_path)
        _upsert_bars(store, "RELIANCE", (1, 2, 3, 6))
        loader = ParquetBacktestLoader(store=store)
        monkeypatch.setattr(
            loader_mod, "load_delistings", lambda *a, **k: {"RELIANCE": date(2026, 7, 2)}
        )

        with pytest.raises(PitViolation, match="2026-07-01"):
            loader.load(instruments=[Equity.of("NSE", "RELIANCE")])

    def test_a_derived_window_is_labelled_as_derived(self, tmp_path, caplog) -> None:
        store = ParquetStorage(tmp_path)
        _upsert_bars(store, "RELIANCE", (1, 2, 3, 6))
        loader = ParquetBacktestLoader(store=store)

        with caplog.at_level(logging.WARNING):
            candles = loader.load(instruments=[Equity.of("NSE", "RELIANCE")])
        assert [c.timestamp for c in candles] == _expected_timestamps()
        assert "window [2026-07-01, 2026-07-06] (derived from the loaded bars)" in caplog.text


class TestTheEntryHalfIsLoadBearing:
    """The entry half can actually fail. These are the non-vacuity proofs.

    Every other test in this file either reports or refuses, and a report is
    only as strong as the assertion behind it. The guard's entry half compares
    the run's resolved selection against the constituent CSV read off disk, so
    a symbol that got into the run without being a member is a violation it
    must raise on. Each test here puts exactly such a symbol in the run and
    asserts the raise — if the manifest were built from the same
    ``load_universe`` call that selected the data (the tautology this work
    removed), none of these would fire, because a set compared with itself
    is always equal.
    """

    def test_a_symbol_the_universe_does_not_hold_refuses_the_load(
        self, tmp_path, monkeypatch
    ) -> None:
        """The canonical case: a run carries a name the index never listed.

        ``load_universe`` is stubbed to hand back a real nifty50 member *plus*
        a name outside it. The selection the loader builds therefore contains
        a symbol the on-disk CSV does not, and the entry half must catch it.
        ``load_universe`` is only the *selection* source here; the manifest is
        built independently by ``universe_snapshot`` reading the file, which
        is precisely why stubbing one does not neuter the guard.
        """
        store = ParquetStorage(tmp_path)
        _upsert_bars(store, _OUTSIDER, (1, 2, 3, 6))
        real = [Equity.of("NSE", sym) for sym in sorted(_NIFTY50_SYMBOLS)]
        monkeypatch.setattr(
            loader_mod, "load_universe", lambda name: [*real, Equity.of("NSE", _OUTSIDER)]
        )
        loader = ParquetBacktestLoader(store=store)

        with pytest.raises(PitViolation) as excinfo:
            loader.load(universe="nifty50", start=_WINDOW[0], end=_WINDOW[1])
        assert _OUTSIDER in str(excinfo.value)

    def test_a_symbol_outside_the_universe_is_caught_even_on_the_derived_window(
        self, tmp_path, monkeypatch
    ) -> None:
        """The unbounded path derives its window from the bars, and the entry
        half still fires — the violation is about the symbol, not the window."""
        store = ParquetStorage(tmp_path)
        _upsert_bars(store, _OUTSIDER, (1, 2, 3, 6))
        monkeypatch.setattr(
            loader_mod,
            "load_universe",
            lambda name: [Equity.of("NSE", _OUTSIDER), Equity.of("NSE", "RELIANCE")],
        )
        loader = ParquetBacktestLoader(store=store)

        with pytest.raises(PitViolation, match=_OUTSIDER):
            loader.load(universe="nifty50")

    def test_the_entry_check_reads_the_csv_not_the_selection_call(
        self, tmp_path, monkeypatch
    ) -> None:
        """The two sides are genuinely independent sources.

        If the manifest were derived from the same ``load_universe`` call as
        the selection, stubbing that call to include an outsider would put the
        outsider on *both* sides of the comparison and the guard would pass.
        Here the stub adds a name the on-disk file does not list, and the guard
        raises — proving the manifest came from the file, not from the stub.
        """
        store = ParquetStorage(tmp_path)
        _upsert_bars(store, _OUTSIDER, (1, 2, 3, 6))
        assert _OUTSIDER not in _NIFTY50_SYMBOLS  # the premise: the file omits it
        monkeypatch.setattr(
            loader_mod,
            "load_universe",
            lambda name: [Equity.of("NSE", _OUTSIDER), Equity.of("NSE", "RELIANCE")],
        )
        loader = ParquetBacktestLoader(store=store)

        with pytest.raises(PitViolation, match=_OUTSIDER):
            loader.load(universe="nifty50", start=_WINDOW[0], end=_WINDOW[1])

    def test_a_member_is_not_falsely_flagged(self, tmp_path, caplog) -> None:
        """The negative control for the entry half.

        A real member loaded on the universe path must pass — if the entry
        check were comparing the wrong things, it would either fire on
        everyone or never fire at all, and the raise-path tests above cannot
        tell those apart. This is the pass that says the guard is selective.
        """
        loader = ParquetBacktestLoader(store=ParquetStorage(tmp_path))

        with caplog.at_level(logging.WARNING):
            loader.load(universe="nifty50", start=_WINDOW[0], end=_WINDOW[1])
        assert "PIT universe check:" in caplog.text
        assert "not in universe manifest" not in caplog.text


class TestTheGuardSaysNothingWhenThereIsNoRun:
    """The two corners where the guard cannot run, stated rather than implied.

    A vacuous pass is only a problem when it reads as a verdict. Neither of
    these loads a bar, so neither is a backtest and neither gets a report; both
    are pinned here so a future change that starts reporting — or worse, starts
    refusing — on them is a visible decision.
    """

    def test_an_empty_selection_is_not_checked(self, tmp_path, caplog) -> None:
        loader = ParquetBacktestLoader(store=ParquetStorage(tmp_path))

        with caplog.at_level(logging.WARNING):
            candles = loader.load(instruments=[])
        assert candles == []
        assert "PIT universe check:" not in caplog.text

    def test_a_load_that_returns_no_bars_is_not_checked(self, tmp_path, caplog) -> None:
        """Unbounded, empty lake: there is no window to derive and no run, so
        there is nothing to check. The pre-existing "no data in window" warning
        is what tells the operator this load measured nothing."""
        loader = ParquetBacktestLoader(store=ParquetStorage(tmp_path))

        with pytest.warns(UserWarning, match="no data in window"):
            with caplog.at_level(logging.WARNING):
                candles = loader.load(instruments=[Equity.of("NSE", "RELIANCE")])
        assert candles == []
        assert "PIT universe check:" not in caplog.text
