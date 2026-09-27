"""Datalake-backed backtest data loader.

Assembles the flat, time-ordered ``Candle`` list that
``BacktestEngine.run(strategy, data)`` consumes directly from the parquet
datalake — offline, across many symbols, no broker APIs involved.

Usage::

    loader = ParquetBacktestLoader()
    candles = loader.load(
        universe="nifty100",
        timeframe=Timeframe.D1,
        start=datetime(2026, 7, 1),
        end=datetime(2026, 7, 31),
    )
    result = BacktestEngine().run(strategy, candles)

    # …or one call from datalake to result, via the replay-side helper
    # ``run_with_loader(loader, strategy, **kwargs)`` — it lives in the
    # replay package, not here, so market_data need not import it.

Point-in-time universe (AC-4)
-----------------------------
Every load asserts its universe is point-in-time correct through
``research.pit.assert_historical_universe``, and then reports what that check
could and could not establish. The guard is here rather than in each caller
because the loader is what the backtest callers actually call — the CLI,
``session.backtest.load()``, ``replay.run_with_loader`` — and one call site
cannot be forgotten by the next caller.

Not covered: ``POST /api/charts/backtest``. That route reads
``ParquetMarketProvider.history`` directly (``interfaces/routes/chart.py``,
``_backtest_candles``) rather than going through this loader, so the chart UI's
backtest button is the one backtest surface with no guard. Closing it means
either routing it through ``load()`` or guarding the provider, and the provider
also serves the chart's own bar requests, which are not backtests — a decision
for whoever owns that route, not something to bolt on here.

The report is not decoration. The checked-in delisting ledger
(``Dependencies/nse_delistings.csv``) has **zero sourced rows**: nothing in this
repository holds historical index membership and no configured broker serves it
for a past date, so the exit side of the check is *unverified* rather than
*clean*, and every load says so. ``point_in_time_report`` and
``universe_snapshot`` are shared with ``trading/scripts/backtest_datalake.py``
so the two surfaces cannot drift into reporting different things.
"""

from __future__ import annotations

import csv
import logging
import warnings
from collections.abc import Mapping, Sequence
from concurrent.futures import ThreadPoolExecutor
from datetime import date, datetime, timedelta
from pathlib import Path
from typing import Any

from tradex_domain.enums import Timeframe
from tradex_domain.instruments import Equity
from tradex_domain.market import Candle
from tradex_domain.trading_calendar import NSETradingCalendar
from tradex_research.manifests import UniverseManifest
from tradex_research.pit import assert_historical_universe, load_delistings

from tradex_market_data.market_provider import ParquetMarketProvider
from tradex_market_data.parquet_storage import ParquetStorage
from tradex_market_data.paths import datalake_root
from tradex_market_data.universe import load_universe

log = logging.getLogger(__name__)

#: The checked-in universe artifacts, package-anchored the way
#: ``tradex_domain.universe`` anchors its own CSV directory (market_data → src →
#: market_data → repo). Naming the ledger is what lets a report say which file
#: it measured rather than printing a bare number.
DEPENDENCIES_DIR = Path(__file__).resolve().parents[3] / "Dependencies"

#: The delisting ledger. Hand-maintained, because it has no other possible
#: source, and **header-only today** — see the module docstring.
DELISTINGS_LEDGER = DEPENDENCIES_DIR / "nse_delistings.csv"

#: Column recording when a constituent snapshot was taken. It is *not* a
#: per-name entry date — a snapshot cannot say when each name joined — so the
#: guard uses it to date the membership it has, never to date a window it does
#: not.
_MEMBERSHIP_COLUMN = "membership_asof"


def universe_snapshot(name: str) -> tuple[tuple[str, ...], date]:
    """``(cash members, snapshot date)`` for a constituent CSV.

    The members come from ``load_universe`` so the cash-series filter stays
    owned by the domain and the guard checks the same set the loader will read.
    The date is the CSV's ``membership_asof`` column, and it dates the
    *snapshot*: it says the membership below is the one that held on that date,
    and nothing about when each name joined.

    :raises ValueError: a snapshot carrying anything other than one distinct
        ``membership_asof`` cannot date itself, and a manifest built from it
        would be claiming a precision the file does not carry.
    """
    members = tuple(i.symbol for i in load_universe(name))
    path = DEPENDENCIES_DIR / f"{name}_list.csv"
    with open(path, newline="", encoding="utf-8") as fh:
        stamps = {(row.get(_MEMBERSHIP_COLUMN) or "").strip() for row in csv.DictReader(fh)}
    stamps.discard("")
    if len(stamps) != 1:
        raise ValueError(
            f"{path}: a universe manifest's as_of_date has to be one date, and this "
            f"snapshot carries {sorted(stamps)} in {_MEMBERSHIP_COLUMN}"
        )
    return members, date.fromisoformat(stamps.pop())


def point_in_time_report(
    *,
    label: str,
    used: int,
    members: int,
    window_start: date,
    window_end: date,
    as_of: date | None,
    delistings: Mapping[str, date | None],
    window_derived: bool = False,
) -> list[str]:
    """The point-in-time report lines for one run — the check's *state*, not its verdict.

    Every line here is a statement about what the check could **not** establish,
    which is the half a reader needs: a run whose exit side rests on an empty
    ledger has not been shown survivorship-free, it has been shown nothing. The
    loader logs these; ``backtest_datalake.py`` prints the same lines, from this
    function, so the two surfaces cannot drift into saying different things.

    :param label: which universe drove the run, and as of when.
    :param used, members: symbols in the run, and symbols the manifest holds.
    :param window_start, window_end: the window checked, both bounds inclusive.
    :param as_of: the snapshot date, or None when the caller named its own
        symbols and no index-membership claim is being made.
    :param delistings: the ledger that was read, so the emptiness is a
        measurement of a named file rather than a constant in a sentence.
    :param window_derived: True when the window came from the loaded bars
        rather than from the caller, which changes what was checked and is
        therefore worth saying.
    """
    ledger = DELISTINGS_LEDGER.relative_to(DEPENDENCIES_DIR.parent)
    derived = " (derived from the loaded bars)" if window_derived else ""
    lines = [
        f"PIT universe check: {label}, {used} of {members} members used, "
        f"window [{window_start.isoformat()}, {window_end.isoformat()}]{derived}"
    ]
    if as_of is None:
        lines.append("  entry side: no index snapshot - the composition is the caller's own list")
    elif window_start < as_of:
        lines.append(
            f"  entry side: UNVERIFIED - the window opens {(as_of - window_start).days}d "
            "before the snapshot date, so the constituents at the window's open are not "
            "the snapshot's and no membership history exists here to establish them"
        )
    else:
        lines.append("  entry side: checked - the window opens on or after the snapshot date")
    if delistings:
        lines.append(
            f"  exit side:  checked against {len(delistings)} sourced delisting(s) in "
            f"{ledger} - none falls inside this window"
        )
    else:
        lines.append(
            f"  exit side:  UNVERIFIED - {ledger} holds 0 delistings, so no delisting "
            "inside this window can be ruled in or out; this is not a clean result"
        )
    return lines


class ParquetBacktestLoader:
    """Build ``BacktestEngine.run`` inputs from ``ParquetStorage`` histories.

    Wraps :class:`ParquetMarketProvider` to turn per-symbol
    ``history(...)`` series into one flat, time-ordered event list — the
    exact contract ``BacktestEngine.run`` expects for multi-symbol runs.
    """

    def __init__(
        self,
        provider: ParquetMarketProvider | None = None,
        store: ParquetStorage | None = None,
        base_path: str | Path | None = None,
        *,
        calendar: NSETradingCalendar | None = None,
        check_gaps: bool = True,
        gap_strict: bool = False,
        delisted: set[str] | None = None,
    ) -> None:
        """Initialize the loader.

        Parameters
        ----------
        provider, store, base_path:
            Data source — a pre-built provider, a store, or a base path
            (mutually exclusive). ``base_path`` resolves to the repo-anchored
            datalake root via ``paths.datalake_root()`` when omitted, so a
            loader built from any working directory opens the same lake
            (the 2026-09-02 incident).
        calendar : NSETradingCalendar | None
            Trading calendar used for gap detection (weekends + exchange
            holidays are not gaps). Defaults to a weekday calendar.
        check_gaps : bool
            Detect interior trading days missing from a symbol's loaded bars
            and warn (parity review #3/#10 — a symbol missing days mid-window
            would silently distort indicators/signals/P&L). Default True.
        gap_strict : bool
            When True, a detected data gap raises instead of warning —
            fail-closed for correctness-critical runs.
        delisted : set[str] | None
            Symbols that have been delisted from the exchange. When loading
            via ``universe`` or the store's full symbol list, any symbol in
            this set is excluded — preventing survivorship bias in backtests
            over past windows (review area #3). Explicit ``instruments`` are
            NOT filtered, so a delisted stock can still be loaded by name.
        """
        # base_path=None lets the provider resolve the anchored root at call
        # time; a default argument would freeze it at import time.
        self._provider = provider or ParquetMarketProvider(
            store=store, base_path=base_path or datalake_root()
        )
        self._calendar = calendar or NSETradingCalendar()
        self._check_gaps = check_gaps
        self._gap_strict = gap_strict
        self._delisted = frozenset(delisted) if delisted else frozenset()

    @property
    def provider(self) -> ParquetMarketProvider:
        """The underlying market provider (per-symbol history queries)."""
        return self._provider

    @property
    def store(self) -> ParquetStorage:
        """The underlying parquet store."""
        return self._provider.store

    def load(
        self,
        instruments: Sequence[Any] | None = None,
        universe: str | None = None,
        timeframe: Timeframe = Timeframe.M1,
        start: datetime | None = None,
        end: datetime | None = None,
        *,
        max_workers: int = 1,
    ) -> list[Candle]:
        """Return a flat, time-ordered candle list for ``BacktestEngine.run``.

        Parameters
        ----------
        instruments : sequence of Instrument | None
            Explicit instruments to load. Mutually exclusive with *universe*.
        universe : str | None
            Nifty universe name (``nifty50`` … ``nifty500``) to load.
            Mutually exclusive with *instruments*.
        timeframe : Timeframe
            Requested bar timeframe; the M1 datalake is resampled as needed.
        start, end : datetime | None
            Window bounds. When omitted, each symbol's stored date range
            (via ``store.date_range``) is used.
        max_workers : int
            Parallelism for per-symbol reads (default 1 = sequential).
            Order is preserved regardless; result equals the sequential call.

        Returns
        -------
        list[Candle]
            One candle per (instrument, bar), sorted by timestamp then
            instrument id so multi-symbol data interleaves chronologically.
            Symbols with no data in the window are skipped with a warning, so
            multi-symbol runs can't silently under-cover the universe.

        Raises
        ------
        PitViolation
            The window is not point-in-time correct — a symbol in the run was
            delisted inside it. Refused, not warned: the bars would be loaded
            and the run would go on to report a survivorship-biased result.

        Notes
        -----
        When a bound is omitted, each symbol's stored date range is read via
        ``store.date_range`` to derive it (one extra read per symbol), and the
        point-in-time check uses the span the loaded bars actually cover — which
        is only known once they are in hand, so that check runs after the read
        and before the candles are returned. A load that returns no bars has no
        window and no run, so it is neither checked nor reported.
        """
        if instruments is not None and universe is not None:
            raise ValueError("pass instruments OR universe, not both")
        insts = self._resolve_instruments(instruments, universe)

        # An explicit window is checked before a single bar is read: a window
        # the ledger already rules out is refused, not loaded and then reported
        # on. The unbounded case has nothing to check yet and is handled below.
        window = (start.date(), end.date()) if start is not None and end is not None else None
        if window is not None:
            self._assert_point_in_time_universe(insts, universe=universe, window=window)

        args = [(inst, start, end, timeframe) for inst in insts]
        if max_workers > 1 and len(args) > 1:
            with ThreadPoolExecutor(max_workers=max_workers) as pool:
                per_inst = list(pool.map(lambda a: self._load_one(*a), args))
        else:
            per_inst = [self._load_one(*a) for a in args]

        candles: list[Candle] = []
        skipped: list[str] = []
        for series_candles, symbol in per_inst:
            if symbol is not None:
                skipped.append(symbol)
            candles.extend(series_candles)

        if skipped:
            warnings.warn(
                f"ParquetBacktestLoader: no data in window for {sorted(set(skipped))}",
                UserWarning,
                stacklevel=2,
            )

        if self._check_gaps:
            self._check_for_gaps(per_inst)

        candles.sort(key=lambda c: (c.timestamp, str(c.instrument.instrument_id)))

        if window is None and candles:
            self._assert_point_in_time_universe(
                insts,
                universe=universe,
                window=(candles[0].timestamp.date(), candles[-1].timestamp.date()),
                window_derived=True,
            )
        return candles

    # ------------------------------------------------------------------ internals

    def _assert_point_in_time_universe(
        self,
        insts: list[Any],
        *,
        universe: str | None,
        window: tuple[date, date],
        window_derived: bool = False,
    ) -> None:
        """Assert the loaded set is point-in-time correct for *window*, then say how far.

        AC-4. The entry side cannot be checked from inside a loader that
        resolved the symbols itself: a manifest built from the same call that
        picked the data is a tautology, so the report states the composition's
        real provenance instead of implying a check that did not happen. The
        exit side can be, and is: a symbol in the run that the ledger says was
        delisted inside the window means the composition at the open differs
        from the composition at the close, and nothing checked in here holds the
        membership that replaced it. So the run is refused.

        A run resolving to no symbols has no backtest to bias, so there is
        nothing to check and nothing to report.

        :raises PitViolation: a symbol delisted inside the window, or one whose
            delisting date was never established.
        """
        symbols = sorted(
            {getattr(inst, "symbol", None) or str(inst.instrument_id) for inst in insts}
        )
        if not symbols:
            return

        as_of: date | None = None
        if universe is None:
            members = tuple(symbols)
            label = "caller-named symbols (no index snapshot)"
        else:
            members, as_of = universe_snapshot(universe)
            label = f"{universe} snapshot as-of {as_of.isoformat()}"

        delistings = load_delistings(DELISTINGS_LEDGER)
        assert_historical_universe(
            UniverseManifest(
                universe_name=universe or "caller-named symbols",
                # No snapshot to date the membership with, so the manifest is
                # dated at the window's close: a caller naming its own symbols
                # claims nothing about the index and gets no claim recorded.
                as_of_date=as_of or window[1],
                symbols=members,
            ),
            symbols,
            window_start=window[0],
            window_end=window[1],
            delistings=delistings,
        )
        log.warning(
            "%s",
            "\n".join(
                point_in_time_report(
                    label=label,
                    used=len(symbols),
                    members=len(members),
                    window_start=window[0],
                    window_end=window[1],
                    as_of=as_of,
                    delistings=delistings,
                    window_derived=window_derived,
                )
            ),
        )

    def _check_for_gaps(
        self, per_inst: list[tuple[list[Candle], str | None]]
    ) -> None:
        """Warn (or raise when *gap_strict*) about interior missing trading days.

        For each symbol with data, find trading days strictly between its
        first and last loaded bar that have no bar — weekends and exchange
        holidays (via the calendar) are not gaps. A symbol's first/last day
        define its own covered range, so a symbol listed mid-window is not
        flagged, and an unfinished final day is not flagged.
        """
        gap_messages: list[str] = []
        for series_candles, symbol in per_inst:
            if symbol is not None or not series_candles:
                continue
            missing = self._missing_trading_days(series_candles)
            if missing:
                label = series_candles[0].instrument.symbol
                dates = ", ".join(d.isoformat() for d in missing)
                gap_messages.append(f"{label}: missing trading day(s) {dates}")
        if not gap_messages:
            return
        message = "ParquetBacktestLoader: data gaps detected — " + "; ".join(
            gap_messages
        )
        if self._gap_strict:
            raise ValueError(message)
        warnings.warn(message, UserWarning, stacklevel=2)

    def _missing_trading_days(self, candles: list[Candle]) -> list[date]:
        """Trading days with no bar strictly inside the loaded date range."""
        days = sorted({c.timestamp.date() for c in candles})
        if len(days) < 2:
            return []
        present = set(days)
        missing: list[date] = []
        day = days[0]
        while day < days[-1]:
            day += timedelta(days=1)
            if self._calendar.is_trading_day(day) and day not in present:
                missing.append(day)
        return missing

    def _load_one(
        self,
        inst: Any,
        start: datetime | None,
        end: datetime | None,
        timeframe: Timeframe,
    ) -> tuple[list[Candle], str | None]:
        """Load one instrument. Returns ``(candles, skipped_symbol_or_None)``.

        Read-only against the store, so it is thread-safe to run under
        ``ThreadPoolExecutor`` when ``max_workers > 1``.
        """
        symbol = getattr(inst, "symbol", None) or str(inst.instrument_id)
        sym_start = start
        sym_end = end
        if sym_start is None or sym_end is None:
            drange = self.store.date_range(symbol)
            if drange is None:
                return [], symbol
            if sym_start is None:
                sym_start = drange[0]
            if sym_end is None:
                sym_end = drange[1]
        series = self._provider.history(inst, timeframe, sym_start, sym_end)
        if not series.candles:
            return [], symbol
        return list(series.candles), None

    def _resolve_instruments(
        self,
        instruments: Sequence[Any] | None,
        universe: str | None,
    ) -> list[Any]:
        """Resolve the instrument set: explicit, universe CSV, or store symbols.

        Delisted symbols (``self._delisted``) are filtered out from
        ``universe`` and store-symbol paths to prevent survivorship bias
        in backtests over past windows. Explicit ``instruments`` are not
        filtered — a delisted symbol can still be loaded by name.
        """
        if instruments is not None:
            seen: dict[Any, Any] = {}
            for inst in instruments:
                seen.setdefault(inst.instrument_id, inst)
            return list(seen.values())
        if universe is not None:
            candidates = load_universe(universe)
            if self._delisted:
                return [
                    inst for inst in candidates
                    if inst.symbol not in self._delisted
                ]
            return candidates
        # No selection → every symbol in the store, as NSE equities.
        store_symbols: list[str] = self.store.symbols()
        if self._delisted:
            store_symbols = [s for s in store_symbols if s not in self._delisted]
        return [Equity.of("NSE", sym) for sym in store_symbols]


__all__ = [
    "DELISTINGS_LEDGER",
    "ParquetBacktestLoader",
    "point_in_time_report",
    "universe_snapshot",
]
