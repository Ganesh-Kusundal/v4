"""AC-9 — every order, fill and position carries the strategy that made it.

The stamp is propagated end-to-end through the real reactive bus (no mocks):
strategy Signal -> PlaceOrderCommand -> ExecutionEngine -> Order -> Fill ->
Position. Legacy/ad-hoc orders (``strategy_id is None``) must survive the
whole path as ``None`` and be grouped separately, never back-filled with a
guess. A negative control asserts an order with no strategy_id is NOT
silently attributed to some strategy.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from decimal import Decimal

from tradex_analytics.trade_metrics import (
    pnl_by_strategy,
    positions_pnl_by_strategy,
)
from tradex_domain import (
    OHLC,
    Candle,
    Fill,
    OrderFilled,
    OrderPlaced,
    PlaceOrderCommand,
    Signal,
)
from tradex_domain.enums import ExchangeId, OrderSide, OrderType, Timeframe
from tradex_domain.execution import OrderRequest, Position
from tradex_domain.instruments import Equity
from tradex_domain.position_math import apply_fill
from tradex_domain.strategy import StrategyContext
from tradex_domain.value_objects import Money, OrderId, Price, Quantity
from tradex_execution.engine import ExecutionEngine
from tradex_execution.fill_sources import SimulatedFillSource
from tradex_reactive.bus import ReactiveBus
from tradex_strategy import ReactiveStrategyEngine

INSTRUMENT = Equity.of(ExchangeId.NSE, "RELIANCE")
SID = "ac9_strategy"


class _BuyEveryBar:
    """Emits a BUY on every bar; the id we assert the stamp against."""

    def __init__(self) -> None:
        self._id = SID

    @property
    def strategy_id(self) -> str:
        return self._id

    def on_start(self, context: StrategyContext) -> None:
        """No-op start hook."""

    def on_stop(self, context: StrategyContext) -> None:
        """No-op stop hook."""

    def on_bar(self, context: StrategyContext, bar: Candle) -> Signal | None:
        return Signal(
            instrument=INSTRUMENT,
            direction=OrderSide.BUY,
            strength=1.0,
            reason="ac9",
        )

    def on_quote(self, context: StrategyContext, quote) -> Signal | None:
        return None

    def on_fill(self, context: StrategyContext, fill: Fill) -> None:
        """No-op fill hook."""


def _candle(day: int) -> Candle:
    price = Price(value=Decimal("2500"))
    return Candle(
        instrument=INSTRUMENT,
        timeframe=Timeframe.D1,
        ohlc=OHLC(open=price, high=price, low=price, close=price),
        volume=Quantity(value=Decimal("1000")),
        timestamp=datetime(2026, 8, 7, tzinfo=UTC) + timedelta(days=day),
    )


def _make_fill(side: OrderSide, price: str, sid: str | None, minute: int) -> Fill:
    return Fill(
        order_id=OrderId(value=f"o{minute}{side.value}"),
        instrument=INSTRUMENT,
        side=side,
        quantity=Quantity(value=Decimal("1")),
        price=Price(value=Decimal(price)),
        timestamp=datetime(2026, 8, 7, tzinfo=UTC) + timedelta(minutes=minute),
        strategy_id=sid,
    )


class TestStrategyIdPropagatesEndToEnd:
    def test_signal_to_order_to_fill_to_position(self) -> None:
        bus = ReactiveBus()
        engine = ExecutionEngine(bus, SimulatedFillSource())
        strategy_engine = ReactiveStrategyEngine(bus)

        placed: list[OrderPlaced] = []
        filled: list[OrderFilled] = []
        bus.of_type(OrderPlaced).subscribe(on_next=placed.append)
        bus.of_type(OrderFilled).subscribe(on_next=filled.append)

        strategy_engine.register(_BuyEveryBar())
        bus.publish(_candle(0))
        bus.publish(_candle(1))  # bar-1 signal flushes at bar-2 open

        assert len(placed) == 1
        assert len(filled) == 1

        # order -> fill
        assert placed[0].order.strategy_id == SID
        assert filled[0].fill.strategy_id == SID

        # fill -> position
        pos = engine.cache.get_position(INSTRUMENT)
        assert pos is not None
        assert pos.strategy_id == SID

        engine.shutdown()
        strategy_engine.dispose_all()

    def test_pure_apply_fill_propagates_strategy(self) -> None:
        """The shared PnL model itself carries the stamp (no engine involved)."""
        fill = _make_fill(OrderSide.BUY, "100", SID, 0)
        pos = apply_fill(None, fill)
        assert pos.strategy_id == SID

        # A reducing fill from a different strategy must not re-attribute the
        # already-open position.
        exit_fill = _make_fill(OrderSide.SELL, "120", "other", 1)
        pos2 = apply_fill(pos, exit_fill)
        assert pos2.strategy_id == SID

    def test_negative_control_no_strategy_id_stays_none(self) -> None:
        """An ad-hoc order with no strategy_id must not be attributed."""
        bus = ReactiveBus()
        engine = ExecutionEngine(bus, SimulatedFillSource())
        placed: list[OrderPlaced] = []
        filled: list[OrderFilled] = []
        bus.of_type(OrderPlaced).subscribe(on_next=placed.append)
        bus.of_type(OrderFilled).subscribe(on_next=filled.append)

        request = OrderRequest(
            instrument=INSTRUMENT,
            side=OrderSide.BUY,
            order_type=OrderType.MARKET,
            quantity=Quantity(value=Decimal("2")),
            price=Price(value=Decimal("2500")),
            # strategy_id omitted (default None)
        )
        bus.publish(PlaceOrderCommand(request=request))

        assert len(placed) == 1 and len(filled) == 1
        assert placed[0].order.strategy_id is None
        assert filled[0].fill.strategy_id is None
        pos = engine.cache.get_position(INSTRUMENT)
        assert pos is not None and pos.strategy_id is None
        engine.shutdown()


class TestPerStrategyPnlGrouping:
    def test_pnl_grouped_by_strategy(self) -> None:
        fills = [
            # strategy A: buy 100, sell 110 -> +10
            _make_fill(OrderSide.BUY, "100", "A", 0),
            _make_fill(OrderSide.SELL, "110", "A", 1),
            # strategy B: buy 50, sell 40 -> -10
            _make_fill(OrderSide.BUY, "50", "B", 2),
            _make_fill(OrderSide.SELL, "40", "B", 3),
        ]
        by_sid = pnl_by_strategy(fills)
        assert by_sid["A"].realized_pnl == Decimal("10")
        assert by_sid["B"].realized_pnl == Decimal("-10")
        assert by_sid["A"].num_round_trips == 1
        assert by_sid["B"].num_round_trips == 1

    def test_lots_do_not_cross_strategies(self) -> None:
        """A sell from B must not close A's open lot (mixed-set guard)."""
        fills = [
            _make_fill(OrderSide.BUY, "100", "A", 0),  # A long 100
            _make_fill(OrderSide.SELL, "200", "B", 1),  # B tries to sell
        ]
        by_sid = pnl_by_strategy(fills)
        # A's fill alone leaves an open long -> no round trip.
        assert by_sid["A"].num_round_trips == 0
        # B's sell alone is an open short -> no round trip.
        assert by_sid["B"].num_round_trips == 0

    def test_none_grouped_separately_not_crashed(self) -> None:
        fills = [
            _make_fill(OrderSide.BUY, "100", None, 0),
            _make_fill(OrderSide.SELL, "90", None, 1),
        ]
        by_sid = pnl_by_strategy(fills)
        assert None in by_sid
        assert by_sid[None].realized_pnl == Decimal("-10")

    def test_positions_grouped_by_strategy(self) -> None:
        def pos(sid, realized):
            return Position(
                instrument=INSTRUMENT,
                quantity=Quantity(value=Decimal("1")),
                avg_price=Price(value=Decimal("100")),
                realized_pnl=Money(amount=Decimal(realized)),
                unrealized_pnl=Money(amount=Decimal("0")),
                strategy_id=sid,
            )

        agg = positions_pnl_by_strategy([pos("A", "5"), pos("B", "-3"), pos(None, "1")])
        assert agg["A"].realized_pnl == Decimal("5")
        assert agg["B"].realized_pnl == Decimal("-3")
        assert agg[None].realized_pnl == Decimal("1")
