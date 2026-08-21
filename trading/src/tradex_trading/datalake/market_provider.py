"""ParquetMarketProvider — market data from the parquet datalake.

Implements the market-provider surface used by ``ScannerEngine`` and
backtest tooling (``history(instrument, timeframe, start, end)``) against
the local ``data/ohlcv`` store instead of broker APIs, so scanning and
backtesting run offline over the full Nifty universe.
"""

from __future__ import annotations

from datetime import datetime
from decimal import Decimal
from pathlib import Path
from typing import Any

from tradex_domain.enums import Timeframe
from tradex_domain.market import Candle, HistoricalSeries

from tradex_trading.datalake.parquet_storage import ParquetStorage


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


__all__ = ["ParquetMarketProvider"]
