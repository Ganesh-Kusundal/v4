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
"""

from __future__ import annotations

import warnings
from collections.abc import Sequence
from concurrent.futures import ThreadPoolExecutor
from datetime import date, datetime, timedelta
from pathlib import Path
from typing import Any

from tradex_domain.enums import Timeframe
from tradex_domain.instruments import Equity
from tradex_domain.market import Candle
from tradex_domain.trading_calendar import NSETradingCalendar

from tradex_market_data.market_provider import ParquetMarketProvider
from tradex_market_data.parquet_storage import ParquetStorage
from tradex_market_data.paths import datalake_root
from tradex_market_data.universe import load_universe


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

        Notes
        -----
        When a bound is omitted, each symbol's stored date range is read via
        ``store.date_range`` to derive it (one extra read per symbol).
        """
        if instruments is not None and universe is not None:
            raise ValueError("pass instruments OR universe, not both")
        insts = self._resolve_instruments(instruments, universe)

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
        return candles

    # ------------------------------------------------------------------ internals

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


__all__ = ["ParquetBacktestLoader"]
