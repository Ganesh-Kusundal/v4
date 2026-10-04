"""Per-symbol event routing (Phase 1).

A strategy declaring ``instruments`` receives only that instrument's
events; a strategy declaring none receives everything (wildcard).
The routing guard must not disturb engine bookkeeping: ``_bar_count``
and ``_flush_pending`` run for every strategy handler exactly as before.
"""

from __future__ import annotations

from datetime import UTC, datetime
from decimal import Decimal

from tradex_domain import OHLC, Candle, Equity, Price, Quantity, Timeframe
from tradex_domain.enums import OrderSide
from tradex_domain.execution import Fill, OrderId
from tradex_domain.market import Quote
from tradex_strategy.core.engine import ReactiveStrategyEngine
from tradex_strategy.core.routing import StrategySubscription, SubscriptionRouter


def _eq(symbol: str) -> Equity:
    return Equity.of("NSE", symbol)


def _candle(symbol: str, ts: datetime | None = None) -> Candle:
    return Candle(
        instrument=_eq(symbol),
        timeframe=Timeframe.M1,
        ohlc=OHLC(
            open=Price(value=Decimal("100")),
            high=Price(value=Decimal("101")),
            low=Price(value=Decimal("99")),
            close=Price(value=Decimal("100")),
        ),
        volume=Quantity(value=Decimal("1000")),
        timestamp=ts or datetime(2026, 8, 5, 10, 0, tzinfo=UTC),
    )


class _RecordingStrategy:
    """Strategy that records which events it received."""

    def __init__(self, strategy_id: str, instruments=None) -> None:
        self._id = strategy_id
        self._instruments = instruments
        self.bars: list = []
        self.quotes: list = []
        self.fills: list = []

    @property
    def strategy_id(self) -> str:
        return self._id

    @property
    def instruments(self):
        return self._instruments

    def on_bar(self, context, candle):
        self.bars.append(candle)
        return None

    def on_quote(self, context, quote):
        self.quotes.append(quote)
        return None

    def on_fill(self, context, fill) -> None:
        self.fills.append(fill)


class _FakeBus:
    """Minimal bus capturing subscribed handlers per event type."""

    def __init__(self) -> None:
        self.handlers: dict[type, list] = {}
        self._event_type: type | None = None

    def of_type(self, event_type):
        self._event_type = event_type
        return self

    def subscribe(self, handler):
        assert self._event_type is not None
        self.handlers.setdefault(self._event_type, []).append(handler)
        return self

    def publish(self, event) -> None:
        for handler in self.handlers.get(type(event), []):
            handler(event)


def _engine(strategies: list[_RecordingStrategy]) -> ReactiveStrategyEngine:
    bus = _FakeBus()
    engine = ReactiveStrategyEngine(bus=bus, submit=lambda req: None)
    for s in strategies:
        engine.register(s)
    return engine


# --- router unit tests ------------------------------------------------------


def test_router_per_instrument_delivery() -> None:
    router = SubscriptionRouter()
    router.subscribe(StrategySubscription(Candle, _eq("RELIANCE").instrument_id), "s1")
    assert router.delivers("s1", Candle, _eq("RELIANCE").instrument_id) is True
    assert router.delivers("s1", Candle, _eq("TCS").instrument_id) is False


def test_router_wildcard_delivery() -> None:
    router = SubscriptionRouter()
    router.subscribe(StrategySubscription(Candle), "s1")
    assert router.delivers("s1", Candle, _eq("RELIANCE").instrument_id) is True
    assert router.delivers("s1", Candle, _eq("TCS").instrument_id) is True


def test_router_unsubscribe_stops_delivery() -> None:
    router = SubscriptionRouter()
    router.subscribe(StrategySubscription(Candle, _eq("RELIANCE").instrument_id), "s1")
    router.unsubscribe("s1")
    assert router.delivers("s1", Candle, _eq("RELIANCE").instrument_id) is False


# --- engine routing tests ---------------------------------------------------


def test_candle_reaches_only_bound_strategy() -> None:
    reliance = _RecordingStrategy("reliance", [_eq("RELIANCE").instrument_id])
    tcs = _RecordingStrategy("tcs", [_eq("TCS").instrument_id])
    engine = _engine([reliance, tcs])
    engine._bus.publish(_candle("RELIANCE"))
    assert len(reliance.bars) == 1
    assert len(tcs.bars) == 0


def test_candle_reaches_universal_strategy() -> None:
    universal = _RecordingStrategy("universal", None)
    engine = _engine([universal])
    engine._bus.publish(_candle("RELIANCE"))
    engine._bus.publish(_candle("TCS"))
    assert len(universal.bars) == 2


def test_bar_count_still_increments_for_all_handlers() -> None:
    """The guard is after bookkeeping: every strategy handler increments
    _bar_count, so with 2 strategies one candle increments it twice."""
    s1 = _RecordingStrategy("s1", [_eq("RELIANCE").instrument_id])
    s2 = _RecordingStrategy("s2", [_eq("TCS").instrument_id])
    engine = _engine([s1, s2])
    engine._bus.publish(_candle("RELIANCE"))
    assert engine._bar_count == 2


def test_quote_reaches_only_bound_strategy() -> None:
    reliance = _RecordingStrategy("reliance", [_eq("RELIANCE").instrument_id])
    tcs = _RecordingStrategy("tcs", [_eq("TCS").instrument_id])
    engine = _engine([reliance, tcs])
    quote = Quote(
        instrument=_eq("RELIANCE"),
        ltp=Price(value=Decimal("100")),
        timestamp=datetime(2026, 8, 5, 10, 0, tzinfo=UTC),
    )
    engine._bus.publish(quote)
    assert len(reliance.quotes) == 1
    assert len(tcs.quotes) == 0


def test_fill_routes_by_owning_strategy() -> None:
    s1 = _RecordingStrategy("s1", [_eq("RELIANCE").instrument_id])
    s2 = _RecordingStrategy("s2", [_eq("TCS").instrument_id])
    engine = _engine([s1, s2])
    fill = Fill(
        order_id=OrderId(value="o1"),
        instrument=_eq("RELIANCE"),
        side=OrderSide.BUY,
        quantity=Quantity(value=Decimal("10")),
        price=Price(value=Decimal("100")),
        strategy_id="s1",
    )
    engine._bus.publish(fill)
    assert len(s1.fills) == 1
    assert len(s2.fills) == 0
