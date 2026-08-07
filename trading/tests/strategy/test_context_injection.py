"""Tests for StrategyContext injection via ReactiveStrategyEngine.

Covers:
- _make_context() creates StrategyContext
- _make_context(bar_count=5) sets bar_count
- _make_context(timestamp=...) sets timestamp
- _wrap_on_bar() passes context to strategy.on_bar
- _wrap_on_quote() passes context to strategy.on_quote
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Any

from tradex_domain.strategy import StrategyContext

from tradex_trading.reactive.bus import ReactiveBus
from tradex_trading.strategy.engine import ReactiveStrategyEngine

# ---------------------------------------------------------------------------
# Recording strategy — captures context passed to callbacks
# ---------------------------------------------------------------------------


@dataclass
class RecordingStrategy:
    """Strategy that records the context it receives."""

    strategy_id: str = "test-strategy"
    contexts: list[StrategyContext] = None  # type: ignore[assignment]

    def __post_init__(self) -> None:
        if self.contexts is None:
            self.contexts = []

    def on_bar(self, ctx: StrategyContext, candle: Any) -> None:
        self.contexts.append(ctx)

    def on_quote(self, ctx: StrategyContext, quote: Any) -> None:
        self.contexts.append(ctx)

    def on_fill(self, ctx: StrategyContext, fill: Any) -> None:
        self.contexts.append(ctx)


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _make_engine() -> ReactiveStrategyEngine:
    bus = ReactiveBus()
    return ReactiveStrategyEngine(bus=bus)


def _now() -> datetime:
    return datetime(2026, 8, 5, 10, 0, tzinfo=UTC)


# ---------------------------------------------------------------------------
# Tests
# ---------------------------------------------------------------------------


def test_make_context_returns_strategy_context() -> None:
    """_make_context() should create a StrategyContext instance."""
    engine = _make_engine()
    ctx = engine._make_context()
    assert isinstance(ctx, StrategyContext)


def test_make_context_with_bar_count() -> None:
    """_make_context(bar_count=5) should set bar_count."""
    engine = _make_engine()
    ctx = engine._make_context(bar_count=5)
    assert ctx.bar_count == 5


def test_make_context_with_timestamp() -> None:
    """_make_context(timestamp=...) should set timestamp."""
    engine = _make_engine()
    ts = _now()
    ctx = engine._make_context(timestamp=ts)
    assert ctx.timestamp == ts


def test_make_context_defaults() -> None:
    """_make_context() with no args should have defaults."""
    engine = _make_engine()
    ctx = engine._make_context()
    assert ctx.bar_count == 0
    assert ctx.timestamp is None


def test_wrap_on_bar_passes_context() -> None:
    """_wrap_on_bar() should pass context with bar_count and timestamp."""
    engine = _make_engine()
    strategy = RecordingStrategy()

    handler = engine._wrap_on_bar(strategy)

    # Simulate a candle with a timestamp
    @dataclass
    class FakeCandle:
        timestamp: datetime = datetime(2026, 8, 5, 10, 0, tzinfo=UTC)

    candle = FakeCandle()
    handler(candle)

    assert len(strategy.contexts) == 1
    ctx = strategy.contexts[0]
    assert isinstance(ctx, StrategyContext)
    assert ctx.bar_count == 1
    assert ctx.timestamp == candle.timestamp


def test_wrap_on_bar_increments_bar_count() -> None:
    """_wrap_on_bar() should increment bar_count on each call."""
    engine = _make_engine()
    strategy = RecordingStrategy()
    handler = engine._wrap_on_bar(strategy)

    @dataclass
    class FakeCandle:
        timestamp: datetime = datetime(2026, 8, 5, 10, 0, tzinfo=UTC)

    handler(FakeCandle())
    handler(FakeCandle())
    handler(FakeCandle())

    assert len(strategy.contexts) == 3
    assert strategy.contexts[0].bar_count == 1
    assert strategy.contexts[1].bar_count == 2
    assert strategy.contexts[2].bar_count == 3


def test_wrap_on_quote_passes_context() -> None:
    """_wrap_on_quote() should pass context with timestamp."""
    engine = _make_engine()
    strategy = RecordingStrategy()
    handler = engine._wrap_on_quote(strategy)

    @dataclass
    class FakeQuote:
        timestamp: datetime = datetime(2026, 8, 5, 10, 30, tzinfo=UTC)

    quote = FakeQuote()
    handler(quote)

    assert len(strategy.contexts) == 1
    ctx = strategy.contexts[0]
    assert isinstance(ctx, StrategyContext)
    assert ctx.timestamp == quote.timestamp
