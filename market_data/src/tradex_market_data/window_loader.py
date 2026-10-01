"""Canonical market-window loader — one door for history/backtest/replay.

Phase 3 of the simulation-architecture work. Chart history, chart backtest and
WS replay each used to resolve a ``(instrument, timeframe, start, end)`` window
independently: three copies of the datalake→broker fallback, three opinions
about empty-lake recovery, and no shared provenance. A parity claim across
those paths was therefore a hope rather than a check.

:class:`MarketWindowLoader` is the single resolution. Callers that need the
serialized chart shape, or a broker session, still own that concern — this
module returns domain candles and says where they came from.
"""

from __future__ import annotations

from collections.abc import Callable, Sequence
from dataclasses import dataclass
from datetime import datetime, timedelta
from enum import StrEnum
from typing import Any

from tradex_domain.enums import Timeframe
from tradex_domain.market import Candle, HistoricalSeries
from tradex_domain.timeframe import candle_epoch
from tradex_domain.timezones import IST_ZONE

from tradex_market_data.market_provider import ParquetMarketProvider
from tradex_market_data.paths import datalake_root


class MarketSource(StrEnum):
    """Where a resolved window's candles came from.

    Stated on every result so a caller that cares (parity gates, audit logs)
    does not have to re-derive it, and so a silent broker fallback cannot be
    mistaken for a datalake hit.
    """

    DATALAKE = "datalake"
    BROKER = "broker"
    NONE = "none"


@dataclass(frozen=True)
class MarketWindow:
    """Result of one window load.

    ``candles`` is empty only when ``source is MarketSource.NONE``. Provenance
    is never inferred by the caller — it travels with the data.
    """

    instrument: Any
    timeframe: Timeframe
    start: datetime
    end: datetime
    candles: tuple[Candle, ...]
    source: MarketSource

    @property
    def empty(self) -> bool:
        return not self.candles


#: Optional broker-history callable: ``(instrument, tf, start, end) -> Sequence[Candle]``.
BrokerHistoryFn = Callable[[Any, Timeframe, datetime, datetime], Sequence[Candle]]


class MarketWindowLoader:
    """Resolve one market window from the datalake, with optional broker fallback.

    Parameters
    ----------
    provider :
        Datalake provider. Defaults to a fresh :class:`ParquetMarketProvider`
        rooted at ``DATALAKE_ROOT``.
    broker_history :
        Optional fallback invoked only when the datalake returns nothing in
        window. Chart history uses this; backtest and replay do not — a
        backtest that silently stretched into live broker data would not be
        reproducible.
    """

    def __init__(
        self,
        provider: ParquetMarketProvider | None = None,
        *,
        broker_history: BrokerHistoryFn | None = None,
        metrics: Any | None = None,
    ) -> None:
        self._provider = provider or ParquetMarketProvider(
            base_path=datalake_root(), metrics=metrics
        )
        self._broker_history = broker_history

    @property
    def provider(self) -> ParquetMarketProvider:
        return self._provider

    def load(
        self,
        instrument: Any,
        timeframe: Timeframe,
        start: datetime,
        end: datetime,
        *,
        allow_broker: bool = False,
    ) -> MarketWindow:
        """Load candles for ``[start, end]`` at *timeframe*.

        Datalake first. Broker only when ``allow_broker`` is set **and** a
        broker fallback was configured **and** the lake is empty in-window.
        """
        series = self._provider.history(instrument, timeframe, start, end)
        candles = tuple(series.candles)
        if candles:
            return MarketWindow(
                instrument=instrument,
                timeframe=timeframe,
                start=start,
                end=end,
                candles=candles,
                source=MarketSource.DATALAKE,
            )
        if allow_broker and self._broker_history is not None:
            try:
                broker_candles = tuple(
                    self._broker_history(instrument, timeframe, start, end)
                )
            except Exception:
                broker_candles = ()
            if broker_candles:
                return MarketWindow(
                    instrument=instrument,
                    timeframe=timeframe,
                    start=start,
                    end=end,
                    candles=broker_candles,
                    source=MarketSource.BROKER,
                )
        return MarketWindow(
            instrument=instrument,
            timeframe=timeframe,
            start=start,
            end=end,
            candles=(),
            source=MarketSource.NONE,
        )

    def history(
        self,
        instrument: Any,
        timeframe: Timeframe,
        start: datetime,
        end: datetime,
        *,
        allow_broker: bool = False,
    ) -> HistoricalSeries:
        """Load history and return as HistoricalSeries (backward-compatible).

        This method provides backward compatibility for callers that need
        a HistoricalSeries instead of a MarketWindow. New code should use
        :meth:`load` to get provenance tracking.

        Parameters
        ----------
        instrument : Instrument
            Instrument to load.
        timeframe : Timeframe
            Requested bar timeframe.
        start, end : datetime
            Inclusive timestamp range.
        allow_broker : bool
            Whether to fall back to broker if datalake is empty.

        Returns
        -------
        HistoricalSeries
            Candles for the requested window; empty series when no data.
        """
        window = self.load(instrument, timeframe, start, end, allow_broker=allow_broker)
        return HistoricalSeries(
            instrument=window.instrument,
            timeframe=window.timeframe,
            candles=list(window.candles),
            start=window.start,
            end=window.end,
        )

    def load_replay_m1(
        self,
        instrument: Any,
        *,
        minutes: int = 390,
        start_time: int | None = None,
        now: datetime | None = None,
    ) -> MarketWindow:
        """Load the M1 window a replay run feeds on.

        Matches the historical WS-route semantics: prefer the trailing
        *minutes* ending at *now*; if that is empty, fall back to the lake's
        own high watermark for the symbol so a stale "now" does not report an
        empty lake as missing. Optional *start_time* (UTC seconds of IST wall
        clock) trims the left edge.
        """
        now = now or datetime.now(IST_ZONE).replace(tzinfo=None)
        if start_time is None:
            from_dt = now - timedelta(minutes=minutes)
        else:
            from_dt = (
                datetime.fromtimestamp(start_time, tz=IST_ZONE).replace(tzinfo=None)
                - timedelta(days=3)
            )
        window = self.load(instrument, Timeframe.M1, from_dt, now)
        if window.empty:
            symbol = getattr(instrument, "symbol", None) or str(
                getattr(instrument, "instrument_id", instrument)
            )
            date_range = self._provider.store.date_range(symbol)
            high = (
                date_range[1]
                if isinstance(date_range, (tuple, list)) and len(date_range) > 1
                else None
            )
            if high is None:
                return window
            now = high if getattr(high, "tzinfo", None) is None else high.replace(tzinfo=None)
            if start_time is None:
                from_dt = now - timedelta(minutes=minutes)
            else:
                from_dt = (
                    datetime.fromtimestamp(start_time, tz=IST_ZONE).replace(tzinfo=None)
                    - timedelta(days=3)
                )
            window = self.load(instrument, Timeframe.M1, from_dt, now)
        candles = window.candles
        if start_time is not None and candles:
            candles = tuple(
                c for c in candles if candle_epoch(c.timestamp) >= start_time
            )
        if not candles:
            return MarketWindow(
                instrument=instrument,
                timeframe=Timeframe.M1,
                start=from_dt,
                end=now,
                candles=(),
                source=MarketSource.NONE,
            )
        candles = tuple(sorted(candles, key=lambda c: candle_epoch(c.timestamp)))
        return MarketWindow(
            instrument=instrument,
            timeframe=Timeframe.M1,
            start=from_dt,
            end=now,
            candles=candles,
            source=window.source,
        )


def series_from_window(window: MarketWindow) -> HistoricalSeries:
    """Wrap a :class:`MarketWindow` as a :class:`HistoricalSeries`."""
    return HistoricalSeries(
        instrument=window.instrument,
        timeframe=window.timeframe,
        candles=list(window.candles),
        start=window.start,
        end=window.end,
    )


__all__ = [
    "BrokerHistoryFn",
    "MarketSource",
    "MarketWindow",
    "MarketWindowLoader",
    "series_from_window",
]

# MarketWindowLoader methods:
# - load(instrument, timeframe, start, end, *, allow_broker=False) -> MarketWindow
# - history(instrument, timeframe, start, end, *, allow_broker=False) -> HistoricalSeries  # noqa: E501
# - load_replay_m1(instrument, *, minutes=390, start_time=None, now=None) -> MarketWindow
