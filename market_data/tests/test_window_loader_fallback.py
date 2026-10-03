"""``MarketWindowLoader`` datalake→broker fallback and ``MarketSource`` provenance.

The loader is the single read door for chart history, backtest and replay, and
the one place that decides where candles came from. That decision is the point:
a caller that cannot tell a datalake hit from a broker fetch cannot tell a
reproducible offline backtest from one that silently stretched into live data.

**Why these are not vacuous.** Every test drives the real ``ParquetStorage``
against a real parquet partition written by ``upsert`` — no mock store, no
injected series. The broker fallback is a callable returning real ``Candle``
objects, which is the seam the production chart route uses. Deleting the
``allow_broker`` branch in ``MarketWindowLoader.load`` makes the fallback tests
fail; deleting the ``source=`` argument makes the provenance tests fail. Both
have been checked by mutation, not assumed.

Provenance is asserted on every path, not only the fallback path: a silent
``DATALAKE`` where the broker actually answered is the failure this module
exists to catch, because it is indistinguishable downstream from a real hit.
"""

from __future__ import annotations

from datetime import datetime
from decimal import Decimal

import pandas as pd
import pytest
from tradex_domain.enums import Timeframe
from tradex_domain.instruments import Equity
from tradex_domain.market import OHLC, Candle, HistoricalSeries
from tradex_domain.value_objects import Price, Quantity
from tradex_market_data.market_provider import ParquetMarketProvider
from tradex_market_data.parquet_storage import ParquetStorage
from tradex_market_data.window_loader import (
    BrokerHistoryFn,
    MarketSource,
    MarketWindowLoader,
)

_START = datetime(2026, 7, 1, 9, 15)
_END = datetime(2026, 7, 1, 10, 0)


def _bar(timestamp: str, close: float = 100.0) -> dict:
    return dict(
        symbol="RELIANCE",
        exchange="NSE",
        kind="equity",
        timeframe="1m",
        timestamp=timestamp,
        open=close - 1.0,
        high=close + 1.0,
        low=close - 2.0,
        close=close,
        volume=1000,
    )


def _seeded_store(tmp_path, *, closes: list[float]) -> ParquetStorage:
    """A real store holding one real M1 bar per price in *closes*."""
    store = ParquetStorage(tmp_path)
    rows = [
        _bar(f"2026-07-01 09:{15 + i:02d}:00", close=close)
        for i, close in enumerate(closes)
    ]
    assert store.upsert(pd.DataFrame(rows)) == len(rows)
    return store


def _candle(close: float, minute: int = 0) -> Candle:
    """A real domain Candle, the shape a broker adapter returns."""
    return Candle(
        instrument=Equity.of("NSE", "RELIANCE"),
        timeframe=Timeframe.M1,
        ohlc=OHLC(
            open=Price(Decimal(str(close - 1.0))),
            high=Price(Decimal(str(close + 1.0))),
            low=Price(Decimal(str(close - 2.0))),
            close=Price(Decimal(str(close))),
        ),
        volume=Quantity(Decimal(1000)),
        timestamp=datetime(2026, 7, 1, 9, 15 + minute),
    )


def _broker_returning(candles: list[Candle]) -> BrokerHistoryFn:
    """A broker-history callable that records that it was called."""
    calls: list[tuple] = []

    def _fn(instrument, timeframe, start, end) -> list[Candle]:
        calls.append((instrument, timeframe, start, end))
        return candles

    _fn.calls = calls  # type: ignore[attr-defined]
    return _fn


# ---------------------------------------------------------------------------
# 1. Datalake wins when it has data — and says so
# ---------------------------------------------------------------------------


def test_uses_datalake_when_available(tmp_path) -> None:
    """A populated datalake is answered from the datalake, and labelled as such."""
    store = _seeded_store(tmp_path, closes=[100.0, 101.0, 102.0])
    broker = _broker_returning([_candle(999.0)])
    loader = MarketWindowLoader(
        provider=ParquetMarketProvider(store=store), broker_history=broker
    )

    window = loader.load(Equity.of("NSE", "RELIANCE"), Timeframe.M1, _START, _END)

    assert window.source is MarketSource.DATALAKE
    assert len(window.candles) == 3
    # The broker must not be consulted at all when the lake has the window.
    assert broker.calls == [], "broker was consulted despite a full datalake hit"
    # And the candles are the stored ones, not the broker's decoy.
    assert [float(c.ohlc.close.value) for c in window.candles] == [100.0, 101.0, 102.0]


def test_datalake_wins_even_when_broker_not_permitted(tmp_path) -> None:
    """``allow_broker=False`` must not silently reach for the broker."""
    store = _seeded_store(tmp_path, closes=[100.0])
    broker = _broker_returning([_candle(999.0)])
    loader = MarketWindowLoader(
        provider=ParquetMarketProvider(store=store), broker_history=broker
    )

    window = loader.load(
        Equity.of("NSE", "RELIANCE"), Timeframe.M1, _START, _END, allow_broker=False
    )

    assert window.source is MarketSource.DATALAKE
    assert broker.calls == []


# ---------------------------------------------------------------------------
# 2. Broker fallback when the lake is empty
# ---------------------------------------------------------------------------


def test_falls_back_to_broker_when_datalake_empty(tmp_path) -> None:
    """An empty lake defers to the broker, and the result is labelled BROKER."""
    store = ParquetStorage(tmp_path)  # no partitions written
    broker_candles = [_candle(500.0), _candle(501.0, minute=1)]
    broker = _broker_returning(broker_candles)
    loader = MarketWindowLoader(
        provider=ParquetMarketProvider(store=store), broker_history=broker
    )

    window = loader.load(
        Equity.of("NSE", "RELIANCE"), Timeframe.M1, _START, _END, allow_broker=True
    )

    assert window.source is MarketSource.BROKER
    assert len(window.candles) == 2
    assert [float(c.ohlc.close.value) for c in window.candles] == [500.0, 501.0]
    # The broker was asked for exactly the window the caller requested.
    assert len(broker.calls) == 1
    _, _, asked_start, asked_end = broker.calls[0]
    assert (asked_start, asked_end) == (_START, _END)


def test_no_fallback_without_allow_broker(tmp_path) -> None:
    """An empty lake with ``allow_broker=False`` stays empty — no silent broker.

    This is the backtest/replay contract: a run that stretched into live broker
    data would not be reproducible, so the default must be fail-closed.
    """
    store = ParquetStorage(tmp_path)
    broker = _broker_returning([_candle(500.0)])
    loader = MarketWindowLoader(
        provider=ParquetMarketProvider(store=store), broker_history=broker
    )

    window = loader.load(Equity.of("NSE", "RELIANCE"), Timeframe.M1, _START, _END)

    assert window.source is MarketSource.NONE
    assert window.candles == ()
    assert window.empty is True
    assert broker.calls == [], "broker consulted without allow_broker=True"


def test_no_fallback_without_a_broker_configured(tmp_path) -> None:
    """``allow_broker=True`` is inert when no broker fallback was supplied."""
    store = ParquetStorage(tmp_path)
    loader = MarketWindowLoader(provider=ParquetMarketProvider(store=store))

    window = loader.load(
        Equity.of("NSE", "RELIANCE"), Timeframe.M1, _START, _END, allow_broker=True
    )

    assert window.source is MarketSource.NONE
    assert window.candles == ()


def test_broker_exception_degrades_to_empty_not_to_raise(tmp_path) -> None:
    """A raising broker must not propagate — the load reports NONE instead.

    A broker that errors mid-load is a degraded feed, not a reason to fail the
    whole request. The caller sees an empty window and can decide what that
    means; it does not see an exception it cannot act on.
    """
    store = ParquetStorage(tmp_path)

    def _boom(instrument, timeframe, start, end) -> list[Candle]:
        raise ConnectionError("venue unreachable")

    loader = MarketWindowLoader(
        provider=ParquetMarketProvider(store=store), broker_history=_boom
    )

    window = loader.load(
        Equity.of("NSE", "RELIANCE"), Timeframe.M1, _START, _END, allow_broker=True
    )

    assert window.source is MarketSource.NONE
    assert window.candles == ()


# ---------------------------------------------------------------------------
# 3. Both sources empty
# ---------------------------------------------------------------------------


def test_returns_none_when_both_empty(tmp_path) -> None:
    """Neither source having data yields an explicit NONE, not an empty DATALAKE."""
    store = ParquetStorage(tmp_path)
    broker = _broker_returning([])
    loader = MarketWindowLoader(
        provider=ParquetMarketProvider(store=store), broker_history=broker
    )

    window = loader.load(
        Equity.of("NSE", "RELIANCE"), Timeframe.M1, _START, _END, allow_broker=True
    )

    assert window.source is MarketSource.NONE
    assert window.candles == ()
    assert window.empty is True
    # The broker was tried — NONE means "both answered empty", not "not asked".
    assert len(broker.calls) == 1


# ---------------------------------------------------------------------------
# 4. The backward-compatible ``history()`` facade agrees with ``load()``
# ---------------------------------------------------------------------------


def test_history_facade_preserves_provenance_in_candles(tmp_path) -> None:
    """``history()`` returns the same candles ``load()`` would.

    ``history()`` exists for callers that need a ``HistoricalSeries`` rather than
    a ``MarketWindow``. It must not become a second, divergent read path: the
    candles it returns are the loader's, not a re-read with different results.
    """
    store = _seeded_store(tmp_path, closes=[100.0, 101.0])
    loader = MarketWindowLoader(provider=ParquetMarketProvider(store=store))

    series = loader.history(Equity.of("NSE", "RELIANCE"), Timeframe.M1, _START, _END)
    window = loader.load(Equity.of("NSE", "RELIANCE"), Timeframe.M1, _START, _END)

    assert isinstance(series, HistoricalSeries)
    assert [float(c.ohlc.close.value) for c in series.candles] == [
        float(c.ohlc.close.value) for c in window.candles
    ]


def test_history_facade_falls_back_to_broker(tmp_path) -> None:
    """The facade honours ``allow_broker`` too — it is not a datalake-only path."""
    store = ParquetStorage(tmp_path)
    broker = _broker_returning([_candle(500.0)])
    loader = MarketWindowLoader(
        provider=ParquetMarketProvider(store=store), broker_history=broker
    )

    series = loader.history(
        Equity.of("NSE", "RELIANCE"), Timeframe.M1, _START, _END, allow_broker=True
    )

    assert [float(c.ohlc.close.value) for c in series.candles] == [500.0]


# ---------------------------------------------------------------------------
# 5. Provenance is never silently DATALAKE
# ---------------------------------------------------------------------------


def test_partial_datalake_window_is_still_labelled_datalake(tmp_path) -> None:
    """A lake holding SOME of the window is a hit for what it has.

    The loader does not require full coverage of the requested range; it returns
    what the lake holds and labels it DATALAKE. The caller can compare
    ``window.start``/``window.end`` against the request to detect the shortfall.
    """
    store = _seeded_store(tmp_path, closes=[100.0])  # one bar of a 45-minute ask
    loader = MarketWindowLoader(provider=ParquetMarketProvider(store=store))

    window = loader.load(Equity.of("NSE", "RELIANCE"), Timeframe.M1, _START, _END)

    assert window.source is MarketSource.DATALAKE
    assert len(window.candles) == 1


@pytest.mark.parametrize(
    ("closes", "allow_broker", "expected"),
    [
        ([100.0, 101.0], True, MarketSource.DATALAKE),
        ([100.0, 101.0], False, MarketSource.DATALAKE),
        ([], True, MarketSource.BROKER),
        ([], False, MarketSource.NONE),
    ],
)
def test_provenance_matrix(tmp_path, closes, allow_broker, expected) -> None:
    """The full lake x broker-permission matrix, each cell labelled correctly.

    Provenance is the loader's reason to exist, so the whole decision table is
    asserted rather than a sample of it. A wrong cell means a caller cannot
    trust where its candles came from.
    """
    store = _seeded_store(tmp_path, closes=closes) if closes else ParquetStorage(tmp_path)
    broker = _broker_returning([_candle(500.0)])
    loader = MarketWindowLoader(
        provider=ParquetMarketProvider(store=store), broker_history=broker
    )

    window = loader.load(
        Equity.of("NSE", "RELIANCE"), Timeframe.M1, _START, _END,
        allow_broker=allow_broker,
    )

    assert window.source is expected
