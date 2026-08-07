"""Tests for enhanced ReplayEngine with strategy registration."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
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


class TestReplayEngineSyntheticTicks:
    """ReplayEngine synthetic mode: M1 candles -> synthetic Quote stream."""

    @staticmethod
    def _m1(ts: datetime) -> Candle:
        return Candle(
            instrument=_eq(),
            timeframe=Timeframe.M1,
            timestamp=ts,
            ohlc=OHLC(
                open=Price(value=Decimal("100")),
                high=Price(value=Decimal("110")),
                low=Price(value=Decimal("95")),
                close=Price(value=Decimal("105")),
            ),
            volume=Quantity(value=Decimal("10000")),
        )

    def test_synthetic_mode_quote_subscribers_see_ticks(self) -> None:
        """Quote-subscribed strategies receive the synthetic tick stream."""
        bus = ReactiveBus()
        seen: list[Quote] = []
        stream: list[object] = []
        bus.of_type(Quote).subscribe(seen.append)
        bus.stream().subscribe(stream.append)
        now = _now()
        engine = ReplayEngine(
            [self._m1(now), self._m1(now + timedelta(minutes=1))],
            synthetic_ticks=True,
            seed=1,
        )
        result = engine.replay(bus)

        assert len(seen) == 120  # 60 ticks per M1 bar
        # Replace semantics: the bus carries only synthetic quotes — no candle.
        assert len(stream) == 120
        assert all(isinstance(m, Quote) for m in stream)
        assert result.candles_processed == 2
        assert result.events_processed == 2
        # Bar 1 timestamps step one second; ticks stay within the bar range.
        for i, quote in enumerate(seen[:60]):
            assert quote.timestamp == now + timedelta(seconds=i)
            assert Decimal("95") <= quote.ltp.value <= Decimal("110")
        assert seen[0].ltp.value == Decimal("100")  # open anchor
        assert seen[59].ltp.value == Decimal("105")  # close anchor

    def test_default_mode_publishes_raw_candles(self) -> None:
        """Without the flag, the bus receives the raw candle (unchanged)."""
        bus = ReactiveBus()
        seen: list[object] = []
        bus.stream().subscribe(seen.append)
        engine = ReplayEngine([self._m1(_now())])
        engine.replay(bus)
        assert len(seen) == 1
        assert isinstance(seen[0], Candle)

    def test_synthetic_mode_clock_seeded_to_first_bar(self) -> None:
        """tick_clock starts at the first candle and advances 1s per tick."""
        now = _now()
        engine = ReplayEngine(
            [self._m1(now), self._m1(now + timedelta(minutes=1))],
            synthetic_ticks=True,
            seed=1,
        )
        assert engine.tick_clock is None  # no replay yet
        engine.replay(ReactiveBus())
        clock = engine.tick_clock
        assert clock is not None
        # Seeded to the first candle's timestamp, advanced 120 ticks.
        assert clock.now() == now + timedelta(seconds=120)

    def test_synthetic_mode_clock_tracks_tick_timestamps(self) -> None:
        """clock.now() sits one tick ahead of the last emitted quote."""
        bus = ReactiveBus()
        quotes: list[Quote] = []
        bus.of_type(Quote).subscribe(quotes.append)
        now = _now()
        engine = ReplayEngine([self._m1(now)], synthetic_ticks=True, seed=1)
        engine.replay(bus)
        clock = engine.tick_clock
        assert clock is not None
        assert len(quotes) == 60
        assert quotes[-1].timestamp == now + timedelta(seconds=59)
        assert clock.now() == quotes[-1].timestamp + timedelta(seconds=1)

    def test_default_mode_has_no_tick_clock(self) -> None:
        """No synthetic mode -> no tick clock."""
        engine = ReplayEngine([self._m1(_now())])
        engine.replay(ReactiveBus())
        assert engine.tick_clock is None

    def test_synthetic_mode_on_bar_still_receives_candle(self) -> None:
        """Registered bar strategies keep working in synthetic mode."""
        bus = ReactiveBus()
        strat = MagicMock()
        engine = ReplayEngine([self._m1(_now())], synthetic_ticks=True, seed=1)
        engine.register_strategy(strat)
        engine.replay(bus)
        strat.on_bar.assert_called_once()

    def test_synthetic_mode_non_m1_candle_recorded_as_error(self) -> None:
        """Non-M1 candles fail loudly in synthetic mode (no silent skip)."""
        bus = ReactiveBus()
        seen: list[object] = []
        bus.stream().subscribe(seen.append)
        engine = ReplayEngine([_candle(100.0, _now())], synthetic_ticks=True)
        result = engine.replay(bus)
        assert seen == []  # nothing published for the D1 candle
        assert result.has_errors
        assert any("Tick generation error" in e for e in result.errors)

    def test_synthetic_mode_deterministic_with_seed(self) -> None:
        """Same seed -> identical tick paths across replays."""
        def prices() -> list[Decimal]:
            bus = ReactiveBus()
            seen: list[Quote] = []
            bus.of_type(Quote).subscribe(seen.append)
            ReplayEngine([self._m1(_now())], synthetic_ticks=True, seed=7).replay(bus)
            return [q.ltp.value for q in seen]

        assert prices() == prices()


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
