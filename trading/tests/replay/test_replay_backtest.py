"""Replay + backtest engine tests (F17).

Ported from v3 ``test_replay_backtest.py``.

v4 API differences:
- ``ReplayEngine(events)`` + ``replay(bus)`` — publishes events through ReactiveBus
- ``BacktestEngine(fill_source=None)`` with ``run(strategy, data)`` → ``BacktestResult``
- ``BacktestResult`` has: total_return, sharpe, max_drawdown, num_trades, trades
- No ``FakeClock`` in v4
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from decimal import Decimal

from tradex_domain import (
    OHLC,
    Candle,
    Equity,
    Price,
    Quantity,
    Quote,
    Timeframe,
)
from tradex_domain.strategy import StrategyContext
from tradex_domain.value_objects import Price as QuotePrice

from tradex_trading.reactive.bus import ReactiveBus
from tradex_trading.replay.backtest import BacktestEngine, BacktestResult
from tradex_trading.replay.engine import ReplayEngine
from tradex_trading.strategy.core.buy_and_hold import BuyAndHoldStrategy


def _now() -> datetime:
    return datetime(2026, 7, 31, 10, 30, tzinfo=UTC)


def _eq() -> Equity:
    return Equity.of("NSE", "RELIANCE")


def _candle(close: float, ts: datetime) -> Candle:
    return Candle(
        instrument=_eq(),
        timeframe=Timeframe.D1,
        ohlc=OHLC(
            open=Price(value=Decimal(str(close - 1))),
            high=Price(value=Decimal(str(close + 1))),
            low=Price(value=Decimal(str(close - 1))),
            close=Price(value=Decimal(str(close))),
        ),
        volume=Quantity(value=Decimal("1000")),
        timestamp=ts,
    )


# ---------------------------------------------------------------------------
# F17 — ReplayEngine: deterministic replay through the bus
# ---------------------------------------------------------------------------


class TestReplayEngine:
    """ReplayEngine publishes events through a ReactiveBus."""

    def test_replay_publishes_events_in_order(self) -> None:
        bus = ReactiveBus()
        seen: list[object] = []
        bus.stream().subscribe(lambda m: seen.append(m))

        events = [{"seq": 0}, {"seq": 1}, {"seq": 2}]
        engine = ReplayEngine(events)
        engine.replay(bus)

        assert len(seen) == 3
        assert [m["seq"] for m in seen] == [0, 1, 2]

    def test_replay_is_deterministic(self) -> None:
        events = [{"seq": i} for i in range(5)]

        bus1 = ReactiveBus()
        seen1: list = []
        bus1.stream().subscribe(lambda m: seen1.append(m))
        ReplayEngine(events).replay(bus1)

        bus2 = ReactiveBus()
        seen2: list = []
        bus2.stream().subscribe(lambda m: seen2.append(m))
        ReplayEngine(events).replay(bus2)

        assert len(seen1) == len(seen2) == 5
        assert [m["seq"] for m in seen1] == [m["seq"] for m in seen2]

    def test_replay_empty_events(self) -> None:
        bus = ReactiveBus()
        seen: list = []
        bus.stream().subscribe(lambda m: seen.append(m))
        ReplayEngine([]).replay(bus)
        assert seen == []

    def test_replay_routes_to_strategy(self) -> None:
        bus = ReactiveBus()
        strategy = BuyAndHoldStrategy("bh-1", _eq())
        ctx = StrategyContext()
        bus.of_type(Candle).subscribe(lambda c: strategy.on_bar(ctx, c))
        bus.of_type(Quote).subscribe(lambda q: strategy.on_quote(ctx, q))

        now = _now()
        events = [
            _candle(100.0, now),
            Quote(instrument=_eq(), ltp=QuotePrice(value=Decimal("100")), timestamp=now),
        ]
        ReplayEngine(events).replay(bus)

        assert len(strategy.signals) == 1  # one quote → one signal


# ---------------------------------------------------------------------------
# F17 — BacktestEngine: strategy over events with metrics
# ---------------------------------------------------------------------------


class TestBacktestEngine:
    """BacktestEngine runs strategies against historical data."""

    def test_backtest_returns_result(self) -> None:
        engine = BacktestEngine()
        strategy = BuyAndHoldStrategy("bh-1", _eq())
        now = _now()
        data = [
            _candle(100.0, now - timedelta(days=2)),
            _candle(101.0, now - timedelta(days=1)),
            _candle(102.0, now),
        ]
        result = engine.run(strategy, data)
        assert isinstance(result, BacktestResult)

    def test_backtest_result_has_metrics(self) -> None:
        engine = BacktestEngine()
        strategy = BuyAndHoldStrategy("bh-1", _eq())
        now = _now()
        data = [_candle(100.0, now)]
        result = engine.run(strategy, data)
        assert isinstance(result.total_return, float)
        assert isinstance(result.sharpe, float)
        assert isinstance(result.max_drawdown, float)
        assert isinstance(result.num_trades, int)

    def test_backtest_with_quotes_counts_trades(self) -> None:
        engine = BacktestEngine()
        strategy = BuyAndHoldStrategy("bh-1", _eq())
        now = _now()
        data = [
            _candle(100.0, now),
            Quote(instrument=_eq(), ltp=QuotePrice(value=Decimal("100")), timestamp=now),
        ]
        result = engine.run(strategy, data)
        # BuyAndHoldStrategy emits 1 signal per quote
        assert result.num_trades == 1

    def test_backtest_empty_data(self) -> None:
        engine = BacktestEngine()
        strategy = BuyAndHoldStrategy("bh-1", _eq())
        result = engine.run(strategy, [])
        assert result.num_trades == 0
