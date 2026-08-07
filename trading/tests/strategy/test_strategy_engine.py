"""Strategy engine + metrics + config tests.

Ported from v3 ``test_followup_runtime_strategy.py``.

v4 API differences:
- ``MetricsRegistry`` only has ``increment/get/snapshot/reset`` (v3 had gauge/histogram/counter)
- ``load_yaml(path)`` returns dict or empty dict (v3 ``load_config`` had strict JSON validation)
- ``ReactiveStrategyEngine(bus)`` replaces v3 ``StrategyEngine``
"""

from __future__ import annotations

from datetime import UTC, datetime
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
from tradex_domain.value_objects import Price as QuotePrice

from tradex_trading.config.loader import load_yaml
from tradex_trading.reactive.bus import ReactiveBus
from tradex_trading.runtime.metrics import MetricsRegistry
from tradex_trading.strategy.core.engine import ReactiveStrategyEngine


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
# MetricsRegistry — counters, snapshot, reset (N11)
# ---------------------------------------------------------------------------


class TestMetricsRegistry:
    """MetricsRegistry — counter/gauge/histogram registry."""

    def test_counter_and_get(self) -> None:
        registry = MetricsRegistry()
        registry.counter("orders").inc()
        registry.counter("orders").inc()
        assert registry.get("orders") == 2

    def test_counter_inc_by_value(self) -> None:
        registry = MetricsRegistry()
        registry.counter("orders").inc(5)
        assert registry.get("orders") == 5

    def test_get_missing_returns_zero(self) -> None:
        registry = MetricsRegistry()
        assert registry.get("nonexistent") == 0

    def test_snapshot(self) -> None:
        registry = MetricsRegistry()
        registry.counter("orders").inc(3)
        registry.counter("fills").inc(2)
        snap = registry.snapshot()
        assert snap == {"orders": 3.0, "fills": 2.0}

    def test_reset(self) -> None:
        registry = MetricsRegistry()
        registry.counter("orders").inc(5)
        registry.reset()
        assert registry.get("orders") == 0
        assert registry.snapshot() == {}


# ---------------------------------------------------------------------------
# Config loader — YAML loading (F22)
# ---------------------------------------------------------------------------


class TestConfigLoader:
    """load_yaml — YAML configuration loader."""

    def test_missing_file_returns_empty(self, tmp_path) -> None:
        result = load_yaml(str(tmp_path / "missing.yaml"))
        assert result == {}

    def test_nonexistent_path_returns_empty(self) -> None:
        result = load_yaml("/nonexistent/path/config.yaml")
        assert result == {}


# ---------------------------------------------------------------------------
# ReactiveStrategyEngine — event routing via bus (F11)
# ---------------------------------------------------------------------------


class _RecordingStrategy:
    """Strategy that records received events."""

    def __init__(self, strategy_id: str = "followup") -> None:
        self._id = strategy_id
        self.bars: list = []
        self.quotes: list = []
        self.fills: list = []

    @property
    def strategy_id(self) -> str:
        return self._id

    def on_bar(self, context, candle: object) -> None:
        self.bars.append(candle)

    def on_quote(self, context, quote: object) -> None:
        self.quotes.append(quote)

    def on_fill(self, context, fill: object) -> None:
        self.fills.append(fill)


class TestReactiveStrategyEngineRouting:
    """ReactiveStrategyEngine routes events through bus to strategies."""

    def test_routes_candle_to_registered_strategy(self) -> None:
        bus = ReactiveBus()
        engine = ReactiveStrategyEngine(bus)
        strat = _RecordingStrategy()
        engine.register(strat)

        candle = _candle(100.0, _now())
        bus.publish(candle)

        assert len(strat.bars) == 1
        engine.dispose_all()

    def test_routes_quote_to_registered_strategy(self) -> None:
        bus = ReactiveBus()
        engine = ReactiveStrategyEngine(bus)
        strat = _RecordingStrategy()
        engine.register(strat)

        quote = Quote(
            instrument=_eq(),
            ltp=QuotePrice(value=Decimal("100")),
            timestamp=_now(),
        )
        bus.publish(quote)

        assert len(strat.quotes) == 1
        engine.dispose_all()

    def test_multiple_strategies_receive_events(self) -> None:
        bus = ReactiveBus()
        engine = ReactiveStrategyEngine(bus)
        s1 = _RecordingStrategy("s1")
        s2 = _RecordingStrategy("s2")
        engine.register(s1)
        engine.register(s2)

        candle = _candle(100.0, _now())
        bus.publish(candle)

        assert len(s1.bars) == 1
        assert len(s2.bars) == 1
        engine.dispose_all()

    def test_unregister_removes_from_strategies(self) -> None:
        bus = ReactiveBus()
        engine = ReactiveStrategyEngine(bus)
        strat = _RecordingStrategy()
        engine.register(strat)
        assert "followup" in engine.strategies

        engine.unregister("followup")
        assert "followup" not in engine.strategies
        engine.dispose_all()

    def test_unregister_missing_is_safe(self) -> None:
        bus = ReactiveBus()
        engine = ReactiveStrategyEngine(bus)
        engine.unregister("nonexistent")  # should not raise
