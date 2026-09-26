"""GapDetector — detect missing symbol/time ranges in the Parquet store.

Compares a requested instrument universe + date range against what already
exists in ``ParquetStorage``, returning the set of instruments that need
their gaps backfilled.

The expected-bar grid is session-aware: only weekday NSE sessions
(09:15–15:30 IST, minus optional exchange holidays) contribute candidate
timestamps, matching what ``ParquetStorage.read`` actually stores/strips.
Without this, nights/weekends/holidays were flagged as gaps and every
symbol looked incomplete (making backfill ``--skip-existing`` a no-op).

**Session-edge stamps are classified, not counted as gaps.** The grid runs
MARKET_OPEN..MARKET_CLOSE, so each session's first and last stamp ("edge"
stamps — 09:15 and 15:30 on a 1m grid) is a stamp the fetch path cannot
deliver reliably: Dhan's ``/charts/intraday`` treats ``fromDate`` as
exclusive, so a request starting at the session open loses the 09:15 bar, and
its NSE 1m series stops at 15:14 — while Upstox's last bar is 15:29. A symbol
whose only shortfall is an edge stamp is therefore a *fetch-shape* finding
rather than a hole in the lake, and is reported as such (``edge_only`` /
``open_missing_symbols`` / ``close_missing_symbols``) instead of becoming a
gap. Folding edges into ``gaps`` is what made every symbol look gapped — the
reason the ``min_gap_stamps`` floor exists — and that floor in turn hid real
holes below it (a 14-stamp tail hole on 2026-08-31 sat under a floor of 15 and
was never fetched). Both are now visible: edges as their own counts, floored
stamps as ``sub_threshold_*``.

The two edges are not equally repairable, so they are not treated alike. The
session **open** IS served (Upstox returns it, and so does Dhan when its window
starts earlier), so ``include_open_stamps=True`` turns a missing 09:15 back
into a fetchable range. The session **close** is served by neither broker's
intraday endpoint, so chasing it would burn a request per symbol-day forever;
it stays a reported count.

``scan()`` returns that full picture; ``detect()`` keeps its original shape
(``[(instrument, [(start, end), ...]), ...]``) and is ``scan().gaps``.

Adapted from nTrade's GapDetector.
"""

from __future__ import annotations

from collections.abc import Iterable
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass, field
from datetime import date, datetime, timedelta
from typing import Any

import pandas as pd
from tradex_domain.market_calendar import MARKET_CLOSE, MARKET_OPEN

from tradex_market_data.parquet_storage import ParquetStorage


def _session_grid(
    start: datetime,
    end: datetime,
    bar_freq: str,
    holidays: frozenset,
) -> pd.DatetimeIndex:
    """Expected bar timestamps for weekday sessions clipped to [start, end].

    Each trading day contributes ``bar_freq``-spaced stamps from
    ``MARKET_OPEN`` to ``MARKET_CLOSE`` (both inclusive, matching
    ``ParquetStorage``'s session filter), intersected with the requested window.

    Window edges are floored onto the ``bar_freq`` grid first: a caller-passed
    ``start`` like ``now - 90d`` (fractional seconds, mid-session) would
    otherwise generate off-grid stamps (e.g. ``12:17:14.706``) that never
    match stored exact-minute bars — flagging ~200 phantom missing bars per
    symbol on the window's first day, every run.
    """
    step = pd.Timedelta(bar_freq)
    start = pd.Timestamp(start).floor(step).to_pydatetime()
    end = pd.Timestamp(end).floor(step).to_pydatetime()

    stamps: list[pd.Timestamp] = []
    day = start.date()
    while day <= end.date():
        if day.weekday() < 5 and day not in holidays:
            lo = max(datetime.combine(day, MARKET_OPEN), start)
            hi = min(datetime.combine(day, MARKET_CLOSE), end)
            if lo <= hi:
                stamps.extend(pd.date_range(start=lo, end=hi, freq=bar_freq))
        day += timedelta(days=1)
    return pd.DatetimeIndex(stamps)


def _edge_stamps(grid: pd.DatetimeIndex) -> tuple[frozenset, frozenset]:
    """Split ``grid`` into (session-open stamps, session-close stamps).

    Read off the grid itself rather than compared against clock times: on a
    30min grid a session's last stamp is 15:15, not ``MARKET_CLOSE``, so a
    ``time == 15:30`` test would classify nothing and silently start reporting
    the last bar of every session as a gap.
    """
    if not len(grid):
        return frozenset(), frozenset()
    frame = pd.Series(pd.DatetimeIndex(grid))
    by_day = frame.groupby(frame.dt.date)
    return frozenset(by_day.min().tolist()), frozenset(by_day.max().tolist())


def _runs(stamps: list, bar_freq: str) -> list[tuple[datetime, datetime]]:
    """Group sorted stamps into maximal contiguous ``(start, end)`` runs."""
    if not stamps:
        return []
    step = pd.Timedelta(bar_freq)
    runs: list[tuple[datetime, datetime]] = []
    run_start = prev = stamps[0]
    for t in stamps[1:]:
        if t != prev + step:
            runs.append((run_start, prev))
            run_start = t
        prev = t
    runs.append((run_start, prev))
    return runs


@dataclass(frozen=True, slots=True)
class GapScan:
    """One universe scan: the actionable gaps, and what only looks like one.

    ``gaps`` keeps ``detect()``'s shape — ``[(instrument, [(start, end), ...])]``.
    """

    gaps: list[tuple] = field(default_factory=list)
    #: Symbols whose ONLY shortfall is session-edge stamps (the intermittently
    #: served 09:15 open / 15:30 close) — never folded into ``gaps`` unless the
    #: scan asked for the open with ``include_open_stamps=True``.
    edge_only: tuple[str, ...] = ()
    #: Scanned symbols missing the session's first / last stamp at all,
    #: whatever else they are missing. The open is repairable, the close is not.
    open_missing_symbols: int = 0
    close_missing_symbols: int = 0
    #: Interior stamps a gap was found for but ``min_gap_stamps`` dropped, and
    #: the symbols holding them. This is the floor's blind spot, counted
    #: instead of silently swallowed.
    sub_threshold_stamps: int = 0
    sub_threshold_symbols: tuple[str, ...] = ()
    #: Scanned symbols with nothing missing — neither interior nor edge.
    complete_symbols: int = 0

    @property
    def gapped_symbols(self) -> int:
        return len(self.gaps)


@dataclass(frozen=True, slots=True)
class _SymbolScan:
    """Per-symbol outcome; the scan aggregates these."""

    symbol: str
    instrument: Any
    gaps: list[tuple]
    edge_only: bool
    open_missing: bool
    close_missing: bool
    sub_threshold_stamps: int


class GapDetector:
    """Detect gaps between a requested universe and stored data.

    Returns a list of ``(instrument, missing_ranges)`` tuples where
    ``missing_ranges`` is a list of ``(start, end)`` datetime pairs.
    """

    def __init__(self, store: ParquetStorage):
        self._store = store

    def detect(
        self,
        instruments: Iterable,
        start: datetime,
        end: datetime,
        timeframe: str = "5m",
        bar_freq: str = "5min",
        holidays: set[date] | None = None,
        min_gap_stamps: int = 1,
        max_workers: int = 1,
        tail_days: int | None = None,
        include_open_stamps: bool = False,
    ) -> list[tuple]:
        """Return instruments with their missing date ranges.

        ``bar_freq`` defines the expected cadence for gap detection.
        ``holidays`` optionally excludes exchange holidays from the expected
        grid (weekday sessions are always included).  ``min_gap_stamps``
        drops detected gaps shorter than N stamps — brokers sometimes serve
        truncated tails or drop stray closing bars; without a floor such
        noise flags every symbol as gapped and ``--skip-existing`` refetches
        the whole universe on every run.  What the floor drops is counted in
        ``scan().sub_threshold_stamps``, so a hole hiding under it is visible.

        ``max_workers`` parallelizes per-symbol parquet reads (read-only).
        ``tail_days`` limits the scan window to ``last_stored - tail_days``
        for symbols with existing data (incremental daily sync).
        ``include_open_stamps`` folds the missing 09:15 open back into the
        returned ranges (see ``scan``) — the wrong default for a bulk sync,
        right for a repair run that wants the open bar back.
        """
        return self.scan(
            instruments, start=start, end=end, timeframe=timeframe,
            bar_freq=bar_freq, holidays=holidays, min_gap_stamps=min_gap_stamps,
            max_workers=max_workers, tail_days=tail_days,
            include_open_stamps=include_open_stamps,
        ).gaps

    def scan(
        self,
        instruments: Iterable,
        start: datetime,
        end: datetime,
        timeframe: str = "5m",
        bar_freq: str = "5min",
        holidays: set[date] | None = None,
        min_gap_stamps: int = 1,
        max_workers: int = 1,
        tail_days: int | None = None,
        include_open_stamps: bool = False,
    ) -> GapScan:
        """Scan the universe once and classify every finding.

        Same window/floor semantics as ``detect``; the difference is that
        nothing is discarded.  A shortfall made up only of session-edge stamps
        lands in ``edge_only``, interior stamps dropped by ``min_gap_stamps``
        are counted in ``sub_threshold_stamps``, and ``gaps`` holds only the
        actionable interior ranges (plus the session-open stamps when
        ``include_open_stamps`` asks for them).
        """
        inst_list = list(instruments)
        if not inst_list:
            return GapScan()
        holiday_set = frozenset(holidays) if holidays else frozenset()

        def _scan_one(inst) -> _SymbolScan | None:
            symbol = inst.symbol
            scan_start = start
            if tail_days is not None:
                last = self.last_stored(symbol)
                if last is not None:
                    tail_start = (last - timedelta(days=tail_days)).replace(
                        hour=0, minute=0, second=0, microsecond=0,
                    )
                    scan_start = max(start, tail_start)
            existing = self._store.read(
                symbols=[symbol], start=scan_start, end=end, timeframe=timeframe,
            )

            if existing.empty:
                return _SymbolScan(
                    symbol=symbol, instrument=inst, gaps=[(start, end)],
                    edge_only=False, open_missing=False, close_missing=False,
                    sub_threshold_stamps=0,
                )

            expected = _session_grid(scan_start, end, bar_freq, holiday_set)
            if not len(expected):
                return None
            existing_set = set(pd.to_datetime(existing["timestamp"]))
            missing = [t for t in expected if t not in existing_set]
            if not missing:
                return None

            open_edges, close_edges = _edge_stamps(expected)
            edge_set = open_edges | close_edges
            interior = [t for t in missing if t not in edge_set]
            opens = [t for t in missing if t in open_edges]

            # Runs over interior stamps only; the session-open stamps join the
            # run set when the caller asked for them (a single missing 09:15
            # must not become a "gap" on its own). The close stamp never
            # joins: no broker serves it, so it can only ever be a wasted
            # request.
            gaps = _runs(interior, bar_freq) if interior else []
            kept: list[tuple[datetime, datetime]] = []
            sub_threshold = 0
            if min_gap_stamps > 1:
                step = pd.Timedelta(bar_freq)
                for gs, ge in gaps:
                    if (ge - gs) // step + 1 >= min_gap_stamps:
                        kept.append((gs, ge))
                    else:
                        sub_threshold += int((ge - gs) // step) + 1
                gaps = kept
            if include_open_stamps:
                gaps = sorted(gaps + _runs(opens, bar_freq))

            return _SymbolScan(
                symbol=symbol,
                instrument=inst,
                gaps=gaps,
                edge_only=not interior,
                open_missing=any(t in open_edges for t in missing),
                close_missing=any(t in close_edges for t in missing),
                sub_threshold_stamps=sub_threshold,
            )

        records: list[_SymbolScan | None]
        if max_workers <= 1:
            records = [_scan_one(inst) for inst in inst_list]
        else:
            with ThreadPoolExecutor(max_workers=max_workers) as pool:
                records = list(pool.map(_scan_one, inst_list))

        out = GapScan()
        edge_only: list[str] = []
        sub_symbols: list[str] = []
        sub_stamps = 0
        open_missing = close_missing = complete = 0
        for rec in records:
            if rec is None:
                complete += 1
                continue
            if rec.gaps:
                out.gaps.append((rec.instrument, rec.gaps))
            elif rec.edge_only:
                edge_only.append(rec.symbol)
            if rec.open_missing:
                open_missing += 1
            if rec.close_missing:
                close_missing += 1
            if rec.sub_threshold_stamps:
                sub_stamps += rec.sub_threshold_stamps
                sub_symbols.append(rec.symbol)
        return GapScan(
            gaps=out.gaps,
            edge_only=tuple(edge_only),
            open_missing_symbols=open_missing,
            close_missing_symbols=close_missing,
            sub_threshold_stamps=sub_stamps,
            sub_threshold_symbols=tuple(sub_symbols),
            complete_symbols=complete,
        )

    def missing_symbols(
        self,
        instruments: Iterable,
        start: datetime,
        end: datetime,
        timeframe: str = "5m",
    ) -> list:
        """Return just the instruments that have zero stored data."""
        return [
            inst for inst in instruments
            if self._store.read(
                symbols=[inst.symbol], start=start, end=end, timeframe=timeframe,
            ).empty
        ]

    def last_stored(self, symbol: str) -> datetime | None:
        """Latest timestamp stored for a symbol, or None."""
        rng = self._store.date_range(symbol)
        return rng[1] if rng else None

    def first_stored(self, symbol: str) -> datetime | None:
        """Earliest timestamp stored for a symbol, or None."""
        rng = self._store.date_range(symbol)
        return rng[0] if rng else None


__all__ = ["GapDetector", "GapScan"]
