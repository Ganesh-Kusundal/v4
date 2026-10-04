"""Tests for the read-only context views (market, position, portfolio).

Phase 6-7: StrategyContext carries read-only views populated by the engine,
so strategies stop reaching through ``context.session`` (a live SessionFacade)
for reads. Covered here:

- the views default to ``None`` (backward compat during migration)
- the engine populates them from its wired providers
- the views are read-only (no mutator surface)
- a strategy reading the views gets the right data
- ``context.session`` still works as a fallback
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from decimal import Decimal
from typing import Any

import pytest
from tradex_domain import (
    OHLC,
    Candle,
    Equity,
    Position,
    Price,
    Quantity,
    Quote,
    Timeframe,
)
from tradex_domain.strategy import StrategyContext
from tradex_domain.value_objects import Money
from tradex_reactive.bus import ReactiveBus
from tradex_strategy.core.engine import ReactiveStrategyEngine

_NOW = datetime(2026, 8, 5, 10, 0, tzinfo=UTC)


def _money(amount: str) -> Money:
    return Money(amount=Decimal(amount))


def _candle(symbol: str = "RELIANCE", i: int = 0) -> Candle:
    ts = _NOW + timedelta(minutes=i)
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


def _quote(symbol: str = "RELIANCE", ltp: str = "1234.5") -> Quote:
    return Quote(
        instrument=Equity.of("NSE", symbol),
        ltp=Price(value=Decimal(ltp)),
        timestamp=_NOW,
    )


def _position(symbol: str = "RELIANCE", qty: str = "10") -> Position:
    return Position(
        instrument=Equity.of("NSE", symbol),
        quantity=Quantity(value=Decimal(qty)),
        avg_price=Price(value=Decimal("100")),
        realized_pnl=_money("0"),
        unrealized_pnl=_money("0"),
    )


class _RecordingStrategy:
    """Captures the context it receives on each callback."""

    strategy_id = "recording"

    @property
    def instruments(self):
        return [Equity.of("NSE", "RELIANCE").instrument_id]

    def __init__(self) -> None:
        self.contexts: list[StrategyContext] = []

    def on_bar(self, context: StrategyContext, candle: Any) -> None:
        self.contexts.append(context)

    def on_quote(self, context: StrategyContext, quote: Any) -> None:
        self.contexts.append(context)


def _engine(**kwargs: Any) -> ReactiveStrategyEngine:
    return ReactiveStrategyEngine(bus=ReactiveBus(), submit=lambda r: None, **kwargs)


# ---------------------------------------------------------------------------
# Defaults (backward compat)
# ---------------------------------------------------------------------------


def test_views_default_to_none() -> None:
    """A context built without providers carries None views, not errors."""
    ctx = StrategyContext()
    assert ctx.market is None
    assert ctx.portfolio is None
    assert ctx.position is None


def test_session_field_still_settable() -> None:
    """context.session survives the migration as a fallback."""
    sentinel = object()
    ctx = StrategyContext(session=sentinel)
    assert ctx.session is sentinel


# ---------------------------------------------------------------------------
# Population
# ---------------------------------------------------------------------------


def test_make_context_populates_views_from_providers() -> None:
    """_make_context with an instrument resolves position + portfolio + market."""
    engine = _engine(
        positions_provider=lambda: [_position()],
        balance_provider=lambda: _money("50000"),
    )
    ctx = engine._make_context(instrument=Equity.of("NSE", "RELIANCE"))
    assert ctx.position is not None
    assert ctx.position.instrument.instrument_id == Equity.of("NSE", "RELIANCE").instrument_id
    assert ctx.portfolio is not None
    assert ctx.portfolio.balance == _money("50000")
    assert len(ctx.portfolio.positions) == 1
    assert ctx.market is not None


def test_make_context_without_providers_leaves_views_none() -> None:
    """No providers → position/portfolio stay None (session fallback path)."""
    engine = _engine()
    ctx = engine._make_context(instrument=Equity.of("NSE", "RELIANCE"))
    assert ctx.position is None
    assert ctx.portfolio is None


def test_position_is_none_for_untracked_instrument() -> None:
    """A position provider with no position for this instrument → None."""
    engine = _engine(positions_provider=lambda: [_position("TCS")])
    ctx = engine._make_context(instrument=Equity.of("NSE", "RELIANCE"))
    assert ctx.position is None


# ---------------------------------------------------------------------------
# Read-only surface
# ---------------------------------------------------------------------------


def test_views_are_read_only() -> None:
    """The views expose no mutator a strategy could call."""
    engine = _engine(
        positions_provider=lambda: [_position()],
        balance_provider=lambda: _money("50000"),
    )
    ctx = engine._make_context(instrument=Equity.of("NSE", "RELIANCE"))
    for view in (ctx.market, ctx.portfolio):
        assert view is not None
        public = {name for name in dir(view) if not name.startswith("_")}
        assert not any(
            name.startswith(("set_", "update", "add_", "remove_", "clear", "pop"))
            for name in public
        ), f"view {type(view).__name__} exposes a mutator: {public}"


def test_market_view_reads_engine_store_read_only() -> None:
    """The view tracks the engine's latest quote and rejects writes."""
    engine = _engine()
    strategy = _RecordingStrategy()
    engine.register(strategy)
    engine._bus.publish(_quote("RELIANCE", "100"))
    ctx = engine._make_context(instrument=Equity.of("NSE", "RELIANCE"))
    assert ctx.market is not None
    assert ctx.market.ltp(Equity.of("NSE", "RELIANCE")) == Price(value=Decimal("100"))
    # A later quote is visible through the same read-only view...
    engine._bus.publish(_quote("RELIANCE", "200"))
    assert ctx.market.ltp(Equity.of("NSE", "RELIANCE")) == Price(value=Decimal("200"))
    # ...but the view itself cannot be written.
    with pytest.raises(TypeError):
        ctx.market._quotes[Equity.of("NSE", "RELIANCE").instrument_id] = _quote()


# ---------------------------------------------------------------------------
# End-to-end: a strategy reading the views
# ---------------------------------------------------------------------------


def test_strategy_reads_views_off_context() -> None:
    """A strategy on the bus sees position/portfolio/market via the context."""
    engine = _engine(
        positions_provider=lambda: [_position()],
        balance_provider=lambda: _money("50000"),
    )
    strategy = _RecordingStrategy()
    engine.register(strategy)
    engine._bus.publish(_quote("RELIANCE", "1234.5"))
    engine._bus.publish(_candle("RELIANCE", 0))

    assert len(strategy.contexts) == 2
    bar_ctx = strategy.contexts[1]
    assert bar_ctx.position is not None
    assert bar_ctx.position.quantity.value == Decimal("10")
    assert bar_ctx.portfolio is not None
    assert bar_ctx.portfolio.balance == _money("50000")
    assert bar_ctx.market is not None
    assert bar_ctx.market.ltp(Equity.of("NSE", "RELIANCE")) == Price(value=Decimal("1234.5"))
