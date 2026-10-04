"""Refactored strategies emit identical signals via the shared view."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from decimal import Decimal

from tradex_analytics.runtime import AnalyticsRuntime
from tradex_domain import OHLC, Candle, Equity, Price, Quantity, Timeframe
from tradex_strategy.core.engine import ReactiveStrategyEngine
from tradex_strategy.extensions.strategies.supertrend_flip import SupertrendFlipStrategy


def _candles(symbol: str, n: int) -> list[Candle]:
    out = []
    for i in range(n):
        ts = datetime(2026, 8, 5, 15, 0, tzinfo=UTC) + timedelta(minutes=i)
        close = 100 + (2.0 * i if i < 20 else 40 - 2.0 * (i - 20))
        out.append(Candle(
            instrument=Equity.of("NSE", symbol),
            timeframe=Timeframe.M1,
            ohlc=OHLC(
                open=Price(value=Decimal(str(close - 1))),
                high=Price(value=Decimal(str(close + 1))),
                low=Price(value=Decimal(str(close - 1))),
                close=Price(value=Decimal(str(close))),
            ),
            volume=Quantity(value=Decimal("1000")),
            timestamp=ts,
        ))
    return out


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


def test_supertrend_flip_signals_match_direct_computation() -> None:
    inst = Equity.of("NSE", "RELIANCE")
    candles = _candles("RELIANCE", 40)

    # Via the shared view (engine + runtime).
    rt = AnalyticsRuntime()
    engine = ReactiveStrategyEngine(bus=_FakeBus(), submit=lambda r: None, analytics=rt)
    via_view = SupertrendFlipStrategy("st_view", inst)
    engine.register(via_view)
    for c in candles:
        engine._bus.publish(c)

    # Direct (no engine): strategy computes locally.
    direct = SupertrendFlipStrategy("st_direct", inst)
    for c in candles:
        direct.on_bar(None, c)

    assert [s.reason for s in via_view.signals] == [s.reason for s in direct.signals]
    assert len(via_view.signals) > 0
