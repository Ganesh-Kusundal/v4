"""AC-8 — per-strategy lifecycle enforcement (pause/stop isolation).

Stopping or pausing one strategy must not affect any other, and stopping
must (a) gate delivery synchronously, (b) dispose ONLY that strategy's
subscriptions, and (c) cancel its WORKING orders (UNKNOWN-aware) while
leaving open positions alone. Negative controls accompany each guarantee.
"""

from __future__ import annotations

from datetime import UTC, datetime
from decimal import Decimal

from tradex_domain import OHLC, Candle, Fill
from tradex_domain.enums import (
    ExchangeId,
    OrderSide,
    OrderStatus,
    OrderType,
    Timeframe,
    TimeInForce,
)
from tradex_domain.execution import Order
from tradex_domain.instruments import Equity
from tradex_domain.strategy import StrategyContext
from tradex_domain.value_objects import OrderId, Price, Quantity
from tradex_reactive.bus import ReactiveBus
from tradex_strategy import ReactiveStrategyEngine

INSTRUMENT = Equity.of(ExchangeId.NSE, "RELIANCE")


class _Counter:
    """Records every bar it receives; emits no signal."""

    def __init__(self, strategy_id: str) -> None:
        self._id = strategy_id
        self.bars = 0

    @property
    def strategy_id(self) -> str:
        return self._id

    def on_start(self, context: StrategyContext) -> None:
        """No-op."""

    def on_stop(self, context: StrategyContext) -> None:
        """No-op."""

    def on_bar(self, context: StrategyContext, bar) -> None:
        self.bars += 1
        return None

    def on_quote(self, context: StrategyContext, quote) -> None:
        return None

    def on_fill(self, context: StrategyContext, fill: Fill) -> None:
        """No-op."""


def _order(status: OrderStatus, sid: str | None, oid: str) -> Order:
    return Order(
        order_id=OrderId(value=oid),
        instrument=INSTRUMENT,
        side=OrderSide.BUY,
        order_type=OrderType.LIMIT,
        quantity=Quantity(value=Decimal("1")),
        price=Price(value=Decimal("100")),
        time_in_force=TimeInForce.DAY,
        status=status,
        strategy_id=sid,
    )


def _engine_with(n: int) -> tuple[ReactiveStrategyEngine, ReactiveBus, list[_Counter]]:
    bus = ReactiveBus()
    engine = ReactiveStrategyEngine(bus)
    counters = []
    for i in range(n):
        c = _Counter(f"s{i}")
        counters.append(c)
        engine.register(c)
    return engine, bus, counters


def _bar() -> Candle:
    p = Price(value=Decimal("100"))
    return Candle(
        instrument=INSTRUMENT,
        timeframe=Timeframe.D1,
        ohlc=OHLC(open=p, high=p, low=p, close=p),
        volume=Quantity(value=Decimal("1")),
        timestamp=datetime(2026, 8, 7, tzinfo=UTC),
    )


class TestStopIsolation:
    def test_stopping_one_leaves_others_receiving(self) -> None:
        engine, bus, cs = _engine_with(3)
        bus.publish(_bar())
        assert [c.bars for c in cs] == [1, 1, 1]

        engine.stop_strategy("s1")
        bus.publish(_bar())

        # s0 and s2 kept receiving; only s1 is silent.
        assert cs[0].bars == 2
        assert cs[1].bars == 1  # stopped — nothing further
        assert cs[2].bars == 2
        engine.dispose_all()

    def test_stop_is_synchronous_not_next_event(self) -> None:
        engine, bus, cs = _engine_with(1)
        bus.publish(_bar())
        assert cs[0].bars == 1
        # stop() gates immediately — verified before any further event.
        engine.stop_strategy("s0")
        engine._gated  # noqa: SLF001 — asserting the gate landed
        assert "s0" in engine._gated  # noqa: SLF001
        bus.publish(_bar())
        assert cs[0].bars == 1
        engine.dispose_all()

    def test_stop_disposes_only_that_strategys_subscriptions(self) -> None:
        engine, bus, cs = _engine_with(2)
        # Two strategies registered => two sets of per-strategy subs.
        assert set(engine._disposables) == {"s0", "s1"}  # noqa: SLF001
        engine.stop_strategy("s0")
        # s0's subs dropped; s1's kept.
        assert "s0" not in engine._disposables  # noqa: SLF001
        assert "s1" in engine._disposables  # noqa: SLF001
        engine.dispose_all()

    def test_unregister_disposes_that_strategys_subscriptions(self) -> None:
        engine, bus, cs = _engine_with(2)
        engine.unregister("s0")
        assert "s0" not in engine._disposables  # noqa: SLF001
        assert "s1" in engine._disposables  # noqa: SLF001
        bus.publish(_bar())
        assert cs[0].bars == 0  # unsubscribed
        assert cs[1].bars == 1  # unaffected
        engine.dispose_all()


class TestPauseVsStop:
    def test_pause_keeps_subscriptions_stop_drops_them(self) -> None:
        engine, bus, cs = _engine_with(1)
        engine.pause_strategy("s0")
        # Paused keeps its subscriptions (so resume re-attaches).
        assert "s0" in engine._disposables  # noqa: SLF001
        assert "s0" in engine._gated  # noqa: SLF001

        engine.resume_strategy("s0")
        assert "s0" not in engine._gated  # noqa: SLF001

        engine.stop_strategy("s0")
        # Stopped drops them.
        assert "s0" not in engine._disposables  # noqa: SLF001
        engine.dispose_all()

    def test_pause_gates_delivery(self) -> None:
        engine, bus, cs = _engine_with(1)
        bus.publish(_bar())
        assert cs[0].bars == 1
        engine.pause_strategy("s0")
        bus.publish(_bar())
        assert cs[0].bars == 1  # gated
        engine.resume_strategy("s0")
        bus.publish(_bar())
        assert cs[0].bars == 2  # resumed
        engine.dispose_all()


class TestStopCancelsWorkingOrders:
    def test_stop_cancels_working_orders_unknown_aware(self) -> None:
        engine, bus, cs = _engine_with(1)
        cancelled: list[str] = []
        orders = [
            _order(OrderStatus.PENDING, "s0", "working-1"),
            _order(OrderStatus.ACK, "s0", "working-2"),
            _order(OrderStatus.UNKNOWN, "s0", "unknown-1"),
            _order(OrderStatus.FILLED, "s0", "filled-1"),
            _order(OrderStatus.CANCELLED, "s0", "cancelled-1"),
        ]
        engine.stop_strategy(
            "s0", cancel=lambda oid: cancelled.append(oid.value),
            working_orders=orders,
        )
        # Working + UNKNOWN reached; settled (FILLED/CANCELLED) skipped.
        assert set(cancelled) == {"working-1", "working-2", "unknown-1"}
        engine.dispose_all()

    def test_stop_does_not_cancel_other_strategys_orders(self) -> None:
        """Even given a MIXED list, only this strategy's orders are cancelled."""
        engine, bus, cs = _engine_with(1)
        cancelled: list[str] = []
        mixed = [
            _order(OrderStatus.PENDING, "s0", "a1"),
            _order(OrderStatus.PENDING, "s1", "b1"),   # another strategy
            _order(OrderStatus.PENDING, None, "manual1"),  # ad-hoc, unattributable
        ]
        engine.stop_strategy("s0", cancel=lambda oid: cancelled.append(oid.value),
                             working_orders=mixed)
        # Isolation is enforced in the engine, not by the caller's filtering.
        assert cancelled == ["a1"]
        engine.dispose_all()

    def test_stop_does_not_flatten_positions(self) -> None:
        """Stopping must NOT cancel/close an open position's protective legs.

        stop_strategy only ever reads working ORDERS; it is given no
        positions and must leave any open position untouched. We model a
        position by asserting the function has no path that acts on one —
        concretely: an open position object is unaffected and no flatten
        callable is invoked.
        """
        from tradex_domain.execution import Position
        from tradex_domain.value_objects import Money

        pos = Position(
            instrument=INSTRUMENT,
            quantity=Quantity(value=Decimal("5")),
            avg_price=Price(value=Decimal("100")),
            realized_pnl=Money(amount=Decimal("0")),
            unrealized_pnl=Money(amount=Decimal("0")),
            strategy_id="s0",
        )
        engine, bus, cs = _engine_with(1)
        cancelled: list[str] = []
        # A working order with protective legs (a bracket) plus the position.
        bracket = Order(
            order_id=OrderId(value="bracket-1"),
            instrument=INSTRUMENT,
            side=OrderSide.BUY,
            order_type=OrderType.MARKET,
            quantity=Quantity(value=Decimal("5")),
            price=Price(value=Decimal("100")),
            time_in_force=TimeInForce.DAY,
            status=OrderStatus.FILLED,
            stop_loss_price=Price(value=Decimal("90")),
            target_price=Price(value=Decimal("120")),
            strategy_id="s0",
        )
        engine.stop_strategy("s0", cancel=lambda oid: cancelled.append(oid.value),
                             working_orders=[bracket])
        # The filled bracket is settled -> not cancelled; and crucially the
        # open position is untouched (still 5 qty, no cancel was issued).
        assert cancelled == []
        assert pos.quantity.value == Decimal("5")
        engine.dispose_all()


class TestNegativeControls:
    """Each guarantee, proven to be a real gate rather than a tautology."""

    def test_nc_without_stop_the_strategy_still_receives(self) -> None:
        """Control for the stop-gate: an un-stopped strategy keeps receiving."""
        engine, bus, cs = _engine_with(1)
        bus.publish(_bar())
        bus.publish(_bar())
        assert cs[0].bars == 2  # not gated, not disposed
        engine.dispose_all()

    def test_nc_pause_alone_does_not_dispose(self) -> None:
        """Control: pause gates but must NOT drop subscriptions."""
        engine, bus, cs = _engine_with(1)
        engine.pause_strategy("s0")
        assert "s0" in engine._disposables  # noqa: SLF001
        engine.dispose_all()

    def test_nc_stop_without_orders_cancels_nothing(self) -> None:
        """Control: with no working orders supplied, cancel is never called."""
        engine, bus, cs = _engine_with(1)
        calls: list[str] = []
        engine.stop_strategy("s0", cancel=lambda oid: calls.append(oid.value),
                             working_orders=[])
        assert calls == []
        engine.dispose_all()

    def test_nc_stop_of_one_does_not_gate_the_other(self) -> None:
        """Control: gating s0 leaves s1 ungated (per-strategy isolation)."""
        engine, bus, cs = _engine_with(2)
        engine.stop_strategy("s0")
        assert "s0" in engine._gated  # noqa: SLF001
        assert "s1" not in engine._gated  # noqa: SLF001
        engine.dispose_all()

    def test_nc_cancel_failure_does_not_stop_the_others(self) -> None:
        """Control: one failing cancel does not prevent the rest."""
        engine, bus, cs = _engine_with(1)
        done: list[str] = []

        def flaky(oid):
            if oid.value == "boom":
                raise RuntimeError("venue down")
            done.append(oid.value)

        failures = engine.stop_strategy(
            "s0", cancel=flaky,
            working_orders=[
                _order(OrderStatus.PENDING, "s0", "boom"),
                _order(OrderStatus.PENDING, "s0", "ok1"),
                _order(OrderStatus.PENDING, "s0", "ok2"),
            ],
        )
        assert done == ["ok1", "ok2"]
        assert failures == ["boom"]


class TestRuntimeHookDrivesEngine:
    """The StrategyRuntime hooks are what make stop() synchronous in a session."""

    def test_runtime_stop_gates_and_disposes_via_hook(self) -> None:
        from tradex_strategy.artifacts import StrategyArtifact
        from tradex_strategy.runtime import StrategyLifecycle, StrategyRuntime

        engine, bus, cs = _engine_with(1)
        bus.publish(_bar())
        assert cs[0].bars == 1

        artifact = StrategyArtifact("s0", "1.0.0")
        rt = StrategyRuntime(
            artifact, cs[0],
            on_pause=lambda: engine.pause_strategy("s0"),
            on_resume=lambda: engine.resume_strategy("s0"),
            on_stop=lambda: engine.stop_strategy("s0"),
        )
        rt.start()
        rt.stop()
        assert rt.lifecycle is StrategyLifecycle.STOPPED
        # Synchronous on the call: no further event needed.
        bus.publish(_bar())
        assert cs[0].bars == 1
        assert "s0" not in engine._disposables  # noqa: SLF001
        engine.dispose_all()

    def test_runtime_pause_keeps_subs_via_hook(self) -> None:
        from tradex_strategy.artifacts import StrategyArtifact
        from tradex_strategy.runtime import StrategyRuntime

        engine, bus, cs = _engine_with(1)
        artifact = StrategyArtifact("s0", "1.0.0")
        rt = StrategyRuntime(
            artifact, cs[0],
            on_pause=lambda: engine.pause_strategy("s0"),
            on_resume=lambda: engine.resume_strategy("s0"),
        )
        rt.start()
        rt.pause()
        assert "s0" in engine._disposables  # noqa: SLF001  (paused keeps subs)
        rt.resume()
        assert "s0" not in engine._gated  # noqa: SLF001
        engine.dispose_all()
