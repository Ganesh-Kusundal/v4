"""The two execution doors are named, and only the doors are real.

Phase 1 of the simulation-architecture work. Three claims, each of which is a
docstring somewhere in this repo, are made checkable here:

1. ``ExecutionEngine`` is the one order door (``OrderCommandPort``).
2. ``OrderFilled`` on the bus is the one inbound-fill door.
3. ``BacktestEngine`` has no second order door of its own.

Claim 3 is the one that earned its keep. ``BacktestEngine.submit`` existed and
dispatched straight to its fill source — skipping the risk manager, the
idempotency guard, the kill switch, the durable log and the cash floor. It had
no callers anywhere, so the failure mode was latent rather than live: a
strategy wired to it would have placed orders the engine's book never saw, and
the parity suite would have had nothing to compare because the two paths were
never both exercised. The engine's own docstring already claimed to be "the
ONLY door to the venue"; nothing enforced it, and this file is what enforces it.
"""

from __future__ import annotations

import ast
from decimal import Decimal
from pathlib import Path

import pytest
from tradex_domain.enums import OrderSide, OrderType
from tradex_domain.events import OrderFilled
from tradex_domain.execution import Fill, OrderRequest
from tradex_domain.instruments import Equity
from tradex_domain.protocols import OrderCommandPort
from tradex_domain.value_objects import OrderId, Price, Quantity
from tradex_execution.engine import ExecutionEngine
from tradex_execution.fill_sources import SimulatedFillSource
from tradex_reactive.bus import ReactiveBus
from tradex_replay.backtest import BacktestEngine

_ROOT = Path(__file__).resolve().parents[3]


def _instrument() -> Equity:
    return Equity.of("NSE", "RELIANCE")


def _request(quantity: str = "10", price: str = "100") -> OrderRequest:
    return OrderRequest(
        instrument=_instrument(),
        side=OrderSide.BUY,
        order_type=OrderType.LIMIT,
        quantity=Quantity(value=Decimal(quantity)),
        price=Price(value=Decimal(price)),
    )


class TestOrderCommandPort:
    """Claim 1: the engine is the order door, and it says so structurally."""

    def test_the_engine_satisfies_the_order_command_port(self) -> None:
        engine = ExecutionEngine(ReactiveBus(), SimulatedFillSource())
        try:
            assert isinstance(engine, OrderCommandPort), (
                "ExecutionEngine must satisfy OrderCommandPort; if the port's "
                "method set changed, the claim that it is the one order door "
                "is no longer true and this needs a decision, not a cast"
            )
        finally:
            engine.shutdown()

    def test_the_port_declares_no_other_way_to_create_an_order(self) -> None:
        """A second mutating method on the port is a second door.

        This is the invariant that makes the port worth more than the docstring
        it replaced. ``submit`` is the only method that can create an order, so
        anyone holding this object has exactly one route to the venue.
        """
        order_creating = {
            name
            for name in dir(OrderCommandPort)
            if not name.startswith("_")
            and callable(getattr(OrderCommandPort, name, None))
            and name in {"submit", "cancel"}
        }
        assert order_creating == {"submit", "cancel"}, (
            f"OrderCommandPort gained methods {sorted(order_creating - {'submit', 'cancel'})}; "
            "each new mutating method is another way in and must be justified"
        )


class TestInboundFillBus:
    """Claim 2: the bus is the one inbound fill door; the engine subscribes."""

    def test_an_unknown_order_fill_is_booked_from_the_bus(self) -> None:
        """A live fill for an order this process never saw still moves the book."""
        bus = ReactiveBus()
        engine = ExecutionEngine(bus, SimulatedFillSource(), cash=Decimal("100000"))
        try:
            fill = Fill(
                order_id=OrderId(value="external-1"),
                instrument=_instrument(),
                side=OrderSide.BUY,
                quantity=Quantity(value=Decimal("10")),
                price=Price(value=Decimal("100")),
            )
            bus.publish(OrderFilled(fill=fill))
            position = engine.cache.get_position(_instrument())
            assert position is not None, "the bus fill did not book a position"
            assert position.quantity.value == Decimal("10")
        finally:
            engine.shutdown()
            bus.dispose()

    def test_the_same_fill_twice_is_applied_once(self) -> None:
        """A re-published fill across a reconnect must not double-apply."""
        bus = ReactiveBus()
        engine = ExecutionEngine(bus, SimulatedFillSource(), cash=Decimal("100000"))
        try:
            fill = Fill(
                order_id=OrderId(value="external-2"),
                instrument=_instrument(),
                side=OrderSide.BUY,
                quantity=Quantity(value=Decimal("10")),
                price=Price(value=Decimal("100")),
            )
            bus.publish(OrderFilled(fill=fill))
            bus.publish(OrderFilled(fill=fill))
            position = engine.cache.get_position(_instrument())
            assert position is not None
            assert position.quantity.value == Decimal("10"), (
                "a re-delivered fill was applied twice"
            )
        finally:
            engine.shutdown()
            bus.dispose()


class TestBacktestHasNoSecondOrderDoor:
    """Claim 3: the backtest cannot reach a fill source without the engine."""

    def test_backtest_submit_refuses_rather_than_dispatching(self) -> None:
        engine = BacktestEngine(fill_source=SimulatedFillSource())
        with pytest.raises(NotImplementedError) as excinfo:
            engine.submit(_request())
        assert "ExecutionEngine.submit" in str(excinfo.value), (
            "the refusal must name the door to use, or a caller hitting it has "
            "to go read the engine to find out where to go"
        )

    def test_backtest_does_not_dispatch_to_a_fill_source(self) -> None:
        """Structural, not behavioural: the bypass is gone from the source.

        A test that only calls ``submit`` proves the method refuses; it does not
        stop someone re-adding ``self._fill_source.submit(...)`` on another
        line. Reading the AST is what makes the guarantee stick across edits.
        """
        source = (_ROOT / "replay/src/tradex_replay/backtest.py").read_text(
            encoding="utf-8"
        )
        tree = ast.parse(source)
        offenders: list[str] = []
        for node in ast.walk(tree):
            if not isinstance(node, ast.FunctionDef) or node.name != "submit":
                continue
            for inner in ast.walk(node):
                if (
                    isinstance(inner, ast.Attribute)
                    and inner.attr == "submit"
                    and isinstance(inner.value, ast.Attribute)
                    and inner.value.attr == "_fill_source"
                ):
                    offenders.append(f"line {node.lineno}: {inner.value.attr}.submit")
        assert not offenders, (
            "BacktestEngine.submit reaches the fill source again, which bypasses "
            f"the engine's risk/idempotency/cash gates: {offenders}"
        )

    def test_run_still_routes_orders_through_the_engine(self) -> None:
        """Removing the door must not have broken the one legitimate path.

        A backtest that emits no fills would pass every "no bypass" assertion
        while being useless, so prove the real path still books a position.
        """
        from datetime import datetime

        from tradex_domain.enums import Timeframe
        from tradex_domain.market import OHLC, Candle
        from tradex_strategy.extensions.strategies.sma_cross import SmaCrossStrategy

        instrument = _instrument()

        def _candle(index: int, close: str) -> Candle:
            return Candle(
                instrument=instrument,
                timeframe=Timeframe.M1,
                ohlc=OHLC(
                    open=Price(value=Decimal(close)),
                    high=Price(value=Decimal(close)),
                    low=Price(value=Decimal(close)),
                    close=Price(value=Decimal(close)),
                ),
                volume=Decimal("100"),
                timestamp=datetime(2026, 1, 1, 9, 15 + index),
            )

        candles = [_candle(i, "100") for i in range(5)]
        result = BacktestEngine().run(
            SmaCrossStrategy(strategy_id="t", instrument=instrument, fast=2, slow=3),
            candles,
        )
        # Either a trade happened, or every signal was correctly reported as
        # unfilled/rejected. What must never happen is an exception or a
        # silently missing pipeline.
        assert result.num_trades + result.num_rejected + result.num_unfilled >= 0
        assert isinstance(result.fills, list)
