"""Scanner timeframe threading (P0).

A ScannerDefinition declares the bar timeframe it evaluates on.
The default (D1) preserves the engine's historical daily-scan
behaviour; an intraday definition must make the engine fetch and
evaluate that timeframe, and the scan cache must key on it.
"""

from __future__ import annotations

from tradex_domain import (
    Condition,
    Equity,
    HistoricalSeries,
    ScannerDefinition,
    Timeframe,
)
from tradex_strategy.core.scanner import ScannerEngine


def _eq(symbol: str = "RELIANCE") -> Equity:
    return Equity.of("NSE", symbol)


class _RecordingMarket:
    """Provider that records the timeframe of every history call."""

    def __init__(self) -> None:
        self.calls: list[tuple[object, Timeframe]] = []

    def history(self, instrument, timeframe, start, end) -> HistoricalSeries:
        self.calls.append((instrument, timeframe))
        return HistoricalSeries(
            instrument=instrument,
            timeframe=timeframe,
            candles=[],
            start=start,
            end=end,
        )


def _condition() -> Condition:
    return Condition(name="close", params={}, operator=">", threshold=1.0)


def test_definition_defaults_to_d1() -> None:
    definition = ScannerDefinition(universe=[_eq()])
    assert definition.timeframe is Timeframe.D1


def test_engine_fetches_definition_timeframe() -> None:
    market = _RecordingMarket()
    engine = ScannerEngine(market=market, min_call_interval=0.0)
    definition = ScannerDefinition(
        universe=[_eq()],
        conditions=[_condition()],
        timeframe=Timeframe.M5,
    )
    engine.run(definition)
    assert market.calls, "engine never called the provider"
    assert all(tf is Timeframe.M5 for _, tf in market.calls)


def test_default_definition_still_fetches_d1() -> None:
    market = _RecordingMarket()
    engine = ScannerEngine(market=market, min_call_interval=0.0)
    engine.run(ScannerDefinition(universe=[_eq()], conditions=[_condition()]))
    assert market.calls
    assert all(tf is Timeframe.D1 for _, tf in market.calls)


def test_cache_key_separates_timeframes() -> None:
    """A D1 scan and an M5 scan of the same definition must not share
    a cache entry — both hit the provider."""
    market = _RecordingMarket()
    engine = ScannerEngine(market=market, min_call_interval=0.0)
    shared = dict(universe=[_eq()], conditions=[_condition()])
    engine.run(ScannerDefinition(timeframe=Timeframe.D1, **shared))
    engine.run(ScannerDefinition(timeframe=Timeframe.M5, **shared))
    timeframes = {tf for _, tf in market.calls}
    assert timeframes == {Timeframe.D1, Timeframe.M5}
