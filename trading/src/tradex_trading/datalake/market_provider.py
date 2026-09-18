"""ParquetMarketProvider — market data from the parquet datalake.

Implements the market-provider surface used by ``ScannerEngine`` and
backtest tooling (``history(instrument, timeframe, start, end)``) against
the local ``data/ohlcv`` store instead of broker APIs, so scanning and
backtesting run offline over the full Nifty universe.
"""

from __future__ import annotations

from datetime import datetime
from pathlib import Path
from typing import Any

import pandas as pd
from tradex_domain.enums import Timeframe
from tradex_domain.market import Candle, HistoricalSeries

from tradex_trading.datalake.parquet_storage import (
    _BASE_COLUMNS,
    ParquetStorage,
)


class ParquetMarketProvider:
    """Serves OHLCV history from ``ParquetStorage``.

    The datalake stores 1-minute bars; requesting another timeframe
    resamples via ``HistoricalSeries.resample()``. Missing symbols return an
    empty series (matching ``ScannerEngine._history``'s degraded contract).
    """

    def __init__(
        self,
        store: ParquetStorage | None = None,
        base_path: str | Path = "data/",
    ) -> None:
        self._store = store or ParquetStorage(base_path)

    @property
    def store(self) -> ParquetStorage:
        """The underlying parquet store (for symbol/daterange queries)."""
        return self._store

    def history(
        self,
        instrument: Any,
        timeframe: Timeframe,
        start: datetime,
        end: datetime,
    ) -> HistoricalSeries:
        """Return OHLCV history for *instrument* over [start, end].

        Parameters
        ----------
        instrument : Instrument
            Instrument to load (its ``symbol`` selects the parquet partition).
        timeframe : Timeframe
            Requested bar timeframe (datalake stores M1; others are resampled).
        start, end : datetime
            Inclusive timestamp range (tz-aware or naive — normalized by the store).

        Returns
        -------
        HistoricalSeries
            Candles for the requested window; empty series when no data.
        """
        symbol = getattr(instrument, "symbol", None) or str(instrument.instrument_id)
        df = self._store.read(symbols=[symbol], start=start, end=end)
        if df.empty:
            return HistoricalSeries(
                instrument=instrument,
                timeframe=timeframe,
                candles=[],
                start=start,
                end=end,
            )
        candles = self._to_candles(instrument, df)
        series = HistoricalSeries(
            instrument=instrument,
            timeframe=Timeframe.M1,
            candles=candles,
            start=start,
            end=end,
        )
        if timeframe != Timeframe.M1:
            series = series.resample(timeframe)
        return series

    # ------------------------------------------------------------------ internals

    @staticmethod
    def _to_candles(instrument: Any, df: Any) -> list[Candle]:
        """Build M1 Candles via single-sourced market_builders helper."""
        from tradex_brokers.common.market_builders import candles_from_dataframe

        return candles_from_dataframe(instrument, df, timeframe=Timeframe.M1)


class BulkPrefetchMarketProvider:
    """Scanner-oriented provider that reads the whole universe in one pass.

    ``ParquetMarketProvider`` serves each instrument with its own
    ``store.read(symbols=[sym])`` — correct but O(universe): every call
    re-resolves Hive partitions and reopens month parquets, so a 500-symbol
    scan pays ~500 cold reads (~100 s). This wrapper issues ONE
    multi-symbol ``store.read()`` up front (partition-pruned across all
    symbols at once) and answers ``history()`` by slicing the prefetched
    frame. Duck-type compatible with ``ScannerEngine._market``.
    """

    def __init__(
        self,
        symbols: list[str],
        start: datetime,
        end: datetime,
        store: ParquetStorage | None = None,
        base_path: str | Path = "data/",
    ) -> None:
        self._store = store or ParquetStorage(base_path)
        self._start = start
        self._end = end
        frame = self._store.read(symbols=list(symbols), start=start, end=end)
        # Group once by symbol; per-instrument lookups are then a dict hit
        # plus an in-memory range slice instead of a filesystem walk.
        if frame.empty:
            empty = frame.iloc[0:0]
            self._by_symbol: dict[str, Any] = {s: empty for s in symbols}
            self._resampled: dict[Timeframe, dict[str, Any]] = {}
            return
        self._by_symbol = {
            str(sym): sub.reset_index(drop=True)
            for sym, sub in frame.groupby("symbol", sort=False)
        }
        missing = set(symbols) - set(self._by_symbol)
        empty = frame.iloc[0:0]
        for sym in missing:
            self._by_symbol[sym] = empty
        # Resampled views (D1 etc.) are built lazily per timeframe: scanners
        # ask every instrument for the same timeframe, so one vectorized
        # aggregation serves all of them instead of N object-level resamples.
        self._resampled = {}

    def history(
        self,
        instrument: Any,
        timeframe: Timeframe,
        start: datetime,
        end: datetime,
    ) -> HistoricalSeries:
        """Return candles for *instrument* from the prefetched frame."""
        symbol = getattr(instrument, "symbol", None) or str(instrument.instrument_id)
        if timeframe != Timeframe.M1:
            df = self._resampled_frame(timeframe).get(symbol)
        else:
            df = self._by_symbol.get(symbol)
        if df is None:
            df = pd.DataFrame(columns=_BASE_COLUMNS)
        # Filter on wall time: prefetched M1 frames carry naive IST
        # timestamps (store convention); resampled views carry UTC-aware
        # ones whose wall values match _bucketize's keys. Guard the .dt
        # access: an empty frame (object dtype) or a non-datetime column
        # raises AttributeError on .dt.
        ts_col = df["timestamp"]
        if not ts_col.empty and pd.api.types.is_datetime64_any_dtype(ts_col):
            if getattr(ts_col.dt, "tz", None) is not None:
                ts_col = ts_col.dt.tz_localize(None)
        lo = pd.Timestamp(start)
        hi = pd.Timestamp(end)
        if lo.tzinfo is not None:
            lo = lo.tz_localize(None)
        if hi.tzinfo is not None:
            hi = hi.tz_localize(None)
        mask = (ts_col >= lo) & (ts_col <= hi)
        window = df.loc[mask]
        if window.empty:
            return HistoricalSeries(
                instrument=instrument,
                timeframe=timeframe,
                candles=[],
                start=start,
                end=end,
            )
        candles = ParquetMarketProvider._to_candles(instrument, window)
        return HistoricalSeries(
            instrument=instrument,
            timeframe=timeframe,
            candles=candles,
            start=start,
            end=end,
        )

    # ------------------------------------------------------------------ internals

    def _resampled_frame(self, timeframe: Timeframe) -> dict[str, Any]:
        """M1 -> target timeframe for every symbol in one vectorized pass.

        Mirrors ``HistoricalSeries.resample``/_bucketize semantics exactly:
        epoch-aligned buckets computed with naive IST wall time treated as
        UTC, open=first / high=max / low=min / close=last, volume summed,
        bucket timestamp = bucket start as UTC. Cached per timeframe because
        a scan requests the same timeframe for the entire universe.
        """
        cached = self._resampled.get(timeframe)
        if cached is not None:
            return cached
        from tradex_domain.timeframe import bucket_seconds

        seconds = bucket_seconds(timeframe)
        all_rows = pd.concat(self._by_symbol.values(), ignore_index=True)
        if all_rows.empty:
            self._resampled[timeframe] = {}
            return {}
        # datetime64 -> whole seconds since epoch, matching
        # calendar.timegm(ts.utctimetuple()) used by _bucketize.
        epoch_s = all_rows["timestamp"].astype("datetime64[s]").astype("int64")
        bucket = (epoch_s // seconds) * seconds
        grouped = all_rows.groupby(
            [all_rows["symbol"], bucket.rename("bucket")], sort=False
        ).agg(
            open=("open", "first"),
            high=("high", "max"),
            low=("low", "min"),
            close=("close", "last"),
            volume=("volume", "sum"),
        )
        grouped = grouped.reset_index()
        # Bucket start rendered as UTC mirrors _bucketize's
        # datetime.fromtimestamp(key, tz=UTC).
        grouped["timestamp"] = pd.to_datetime(grouped["bucket"], unit="s", utc=True)
        grouped = grouped.sort_values(["symbol", "timestamp"]).reset_index(drop=True)
        by_symbol = {
            str(sym): sub[["timestamp", "open", "high", "low", "close", "volume"]]
            .reset_index(drop=True)
            for sym, sub in grouped.groupby("symbol", sort=False)
        }
        self._resampled[timeframe] = by_symbol
        return by_symbol


__all__ = ["BulkPrefetchMarketProvider", "ParquetMarketProvider"]
