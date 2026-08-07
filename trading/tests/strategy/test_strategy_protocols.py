"""Tests for strategy protocol and buy-and-hold strategy.

Tests the ported on_start(), on_stop(), on_event() methods.
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
    Timeframe,
)
from tradex_domain.strategy import StrategyContext

from tradex_trading.strategy.core.buy_and_hold import BuyAndHoldStrategy
from tradex_trading.strategy.core.protocols import Strategy


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
# Strategy Protocol
# ---------------------------------------------------------------------------


class TestStrategyProtocol:
    """Strategy protocol defines the contract for trading strategies."""

    def test_buy_and_hold_satisfies_protocol(self) -> None:
        """BuyAndHoldStrategy should satisfy the Strategy protocol."""
        strategy = BuyAndHoldStrategy("test", _eq())
        assert isinstance(strategy, Strategy)

    def test_protocol_has_on_start(self) -> None:
        """Strategy protocol should have on_start method."""
        assert hasattr(Strategy, "on_start")

    def test_protocol_has_on_stop(self) -> None:
        """Strategy protocol should have on_stop method."""
        assert hasattr(Strategy, "on_stop")

    def test_protocol_has_on_event(self) -> None:
        """Strategy protocol should have on_event method."""
        assert hasattr(Strategy, "on_event")


# ---------------------------------------------------------------------------
# BuyAndHoldStrategy — lifecycle methods
# ---------------------------------------------------------------------------


class TestBuyAndHoldLifecycle:
    """BuyAndHoldStrategy lifecycle methods (on_start, on_stop, on_event)."""

    def test_on_start_does_not_raise(self) -> None:
        """on_start should not raise."""
        strategy = BuyAndHoldStrategy("test", _eq())
        ctx = StrategyContext()
        strategy.on_start(ctx)  # Should not raise

    def test_on_stop_does_not_raise(self) -> None:
        """on_stop should not raise."""
        strategy = BuyAndHoldStrategy("test", _eq())
        ctx = StrategyContext()
        strategy.on_stop(ctx)  # Should not raise

    def test_on_event_does_not_raise(self) -> None:
        """on_event should not raise."""
        strategy = BuyAndHoldStrategy("test", _eq())
        strategy.on_event({"type": "custom"})
        strategy.on_event("any_event")

    def test_on_start_returns_none(self) -> None:
        """on_start should return None."""
        strategy = BuyAndHoldStrategy("test", _eq())
        result = strategy.on_start(StrategyContext())
        assert result is None

    def test_on_stop_returns_none(self) -> None:
        """on_stop should return None."""
        strategy = BuyAndHoldStrategy("test", _eq())
        result = strategy.on_stop(StrategyContext())
        assert result is None

    def test_on_event_returns_none(self) -> None:
        """on_event should return None."""
        strategy = BuyAndHoldStrategy("test", _eq())
        result = strategy.on_event("event")
        assert result is None

    def test_lifecycle_order(self) -> None:
        """Test typical lifecycle: start -> bar/quote -> stop."""
        strategy = BuyAndHoldStrategy("test", _eq())
        now = _now()
        ctx = StrategyContext()

        strategy.on_start(ctx)
        strategy.on_bar(ctx, _candle(100.0, now))
        strategy.on_event({"type": "custom"})
        strategy.on_stop(ctx)

        # Should not raise and should work normally
