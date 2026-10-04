"""Engine ingests candles into the analytics runtime and passes the view."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from decimal import Decimal

from tradex_analytics.runtime import AnalyticsRuntime
from tradex_domain import OHLC, Candle, Equity, Price, Quantity, Timeframe
from tradex_strategy.core.engine import ReactiveStrategyEngine


def _candle(symbol: str, i: int) -> Candle:
    ts = datetime(2026, 8, 5, 15, 0, tzinfo=UTC) + timedelta(minutes=i)
    return Candle(
        instrument=Equity.of("NSE", symbol),
        timeframe=Timeframe.M1,
        ohlc=OHLC(
            open=Price(value=Decimal("100")),
            high=Price(value=Decimal("101")),
            low=Price(value=Decimal("99")),
            close=Price(value=Decimal(str(100 + i))),
        ),
        volume=Quantity(value=Decimal("1000")),
        timestamp=ts,
    )


class _ViewCheckStrategy:
    """Records whether context.analytics was present on each bar."""

    def __init__(self) -> None:
        self.views: list = []

    @property
    def strategy_id(self) -> str:
        return "view_check"

    @property
    def instruments(self):
        return [Equity.of("NSE", "RELIANCE").instrument_id]

    def on_bar(self, context, candle):
        self.views.append(context.analytics)
        return None


def test_engine_passes_analytics_view() -> None:
    rt = AnalyticsRuntime()
    engine = ReactiveStrategyEngine(bus=_FakeBus(), submit=lambda r: None, analytics=rt)
    strategy = _ViewCheckStrategy()
    engine.register(strategy)
    engine._bus.publish(_candle("RELIANCE", 0))
    assert len(strategy.views) == 1
    assert strategy.views[0] is not None


def test_engine_without_analytics_passes_none_view() -> None:
    engine = ReactiveStrategyEngine(bus=_FakeBus(), submit=lambda r: None)
    strategy = _ViewCheckStrategy()
    engine.register(strategy)
    engine._bus.publish(_candle("RELIANCE", 0))
    assert strategy.views[0] is None


class _FakeSubscription:
    """Returned by ``of_type``; captures the event type for dispatch."""

    def __init__(self, bus: _FakeBus, event_type: type) -> None:
        self._bus = bus
        self._event_type = event_type

    def subscribe(self, handler):
        self._bus.handlers.setdefault(self._event_type, []).append(handler)
        return self


class _FakeBus:
    def __init__(self) -> None:
        self.handlers: dict[type, list] = {}

    def of_type(self, event_type):
        return _FakeSubscription(self, event_type)

    def publish(self, event) -> None:
        for handler in self.handlers.get(type(event), []):
            handler(event)
