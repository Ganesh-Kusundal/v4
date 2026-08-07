"""Tests for enhanced ReplayEngine with strategy registration."""

from __future__ import annotations

from datetime import UTC, datetime
from decimal import Decimal
from unittest.mock import MagicMock

from tradex_domain import OHLC, Candle, Equity, Price, Quantity, Quote, Timeframe

from tradex_trading.reactive.bus import ReactiveBus
from tradex_trading.replay.engine import ReplayEngine, ReplayResult


def _now() -> datetime:
    return datetime(2026, 7, 31, 10, 30, tzinfo=UTC)


def _eq() -> Equity:
    return Equity.of("NSE", "RELIANCE")


def _candle(price: float, ts: datetime) -> Candle:
    return Candle(
        instrument=_eq(),
        timeframe=Timeframe.D1,
        timestamp=ts,
        ohlc=OHLC(
            open=Price(value=Decimal(str(price))),
            high=Price(value=Decimal(str(price * 1.01))),
            low=Price(value=Decimal(str(price * 0.99))),
            close=Price(value=Decimal(str(price))),
        ),
        volume=Quantity(value=Decimal("1000")),
    )


class TestReplayResult:
    def test_default_values(self):
        r = ReplayResult()
        assert r.events_processed == 0
        assert r.candles_processed == 0
        assert r.quotes_processed == 0
        assert r.fills_processed == 0
        assert r.errors == []

    def test_has_errors_false(self):
        r = ReplayResult()
        assert not r.has_errors

    def test_has_errors_true(self):
        r = ReplayResult(errors=["boom"])
        assert r.has_errors

    def test_repr(self):
        r = ReplayResult(events_processed=10, candles_processed=5)
        s = repr(r)
        assert "events=10" in s
        assert "candles=5" in s


class TestReplayEngineEnhanced:
    def test_register_strategy(self):
        engine = ReplayEngine([])
        strat = MagicMock()
        result = engine.register_strategy(strat)
        assert result is engine  # chaining
        assert strat in engine.strategies

    def test_unregister_strategy(self):
        engine = ReplayEngine([])
        strat = MagicMock()
        engine.register_strategy(strat)
        engine.unregister_strategy(strat)
        assert strat not in engine.strategies

    def test_replay_calls_on_start(self):
        bus = ReactiveBus()
        strat = MagicMock()
        engine = ReplayEngine([])
        engine.register_strategy(strat)
        engine.replay(bus)
        strat.on_start.assert_called_once()

    def test_replay_calls_on_stop(self):
        bus = ReactiveBus()
        strat = MagicMock()
        engine = ReplayEngine([])
        engine.register_strategy(strat)
        engine.replay(bus)
        strat.on_stop.assert_called_once()

    def test_replay_candle_calls_on_bar(self):
        bus = ReactiveBus()
        now = _now()
        events = [_candle(100.0, now)]
        strat = MagicMock()
        engine = ReplayEngine(events)
        engine.register_strategy(strat)
        result = engine.replay(bus)
        strat.on_bar.assert_called_once()
        assert result.candles_processed == 1

    def test_replay_quote_calls_on_quote(self):
        bus = ReactiveBus()
        now = _now()
        quote = Quote(instrument=_eq(), ltp=Price(value=Decimal("100")), timestamp=now)
        events = [quote]
        strat = MagicMock()
        engine = ReplayEngine(events)
        engine.register_strategy(strat)
        result = engine.replay(bus)
        strat.on_quote.assert_called_once()
        assert result.quotes_processed == 1

    def test_replay_result_counts(self):
        bus = ReactiveBus()
        now = _now()
        events = [
            _candle(100.0, now),
            _candle(101.0, now),
            Quote(instrument=_eq(), ltp=Price(value=Decimal("100")), timestamp=now),
        ]
        engine = ReplayEngine(events)
        result = engine.replay(bus)
        assert result.events_processed == 3
        assert result.candles_processed == 2
        assert result.quotes_processed == 1

    def test_replay_without_bus(self):
        events = [_candle(100.0, _now())]
        strat = MagicMock()
        engine = ReplayEngine(events)
        engine.register_strategy(strat)
        result = engine.replay()  # no bus
        assert result.events_processed == 1

    def test_replay_strategy_exception_captured(self):
        bus = ReactiveBus()
        now = _now()
        events = [_candle(100.0, now)]
        strat = MagicMock()
        strat.on_bar.side_effect = RuntimeError("boom")
        engine = ReplayEngine(events)
        engine.register_strategy(strat)
        result = engine.replay(bus)
        assert result.has_errors
        assert any("on_bar" in e for e in result.errors)

    def test_replay_multiple_strategies(self):
        bus = ReactiveBus()
        now = _now()
        events = [_candle(100.0, now)]
        s1 = MagicMock()
        s2 = MagicMock()
        engine = ReplayEngine(events)
        engine.register_strategy(s1)
        engine.register_strategy(s2)
        engine.replay(bus)
        s1.on_bar.assert_called_once()
        s2.on_bar.assert_called_once()
