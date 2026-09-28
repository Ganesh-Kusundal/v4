"""AC-22: ``ExecutionEngine.submit()`` is the only path to the venue.

The hole AC-22 closes. Before it, ``ExecutionEngine._setup_pipeline``
subscribed the engine to TWO event types on the public bus:

    bus.of_type(OrderRequest).subscribe(self._process_request)
    bus.of_type(PlaceOrderCommand).subscribe(lambda c: self._process_request(c.request))

Either one was enough to put an order in front of the venue. The CQRS
subscription in particular was the door the spec names: any code holding a bus
reference could ``bus.publish(PlaceOrderCommand(request=...))`` and reach the
fill source.

What that actually skipped is worth stating precisely, because the spec
overstates it. Both subscriptions converged on ``_process_request`` →
``_run_pipeline``, so the ENGINE-level gates — kill switch, feed readiness,
idempotency, risk, the cash floor — ran exactly as they do for ``submit()``.
The gate a bus publisher skipped was in the STRATEGY layer, above the engine:

  * ``PositionSizer`` (``tradex_strategy.core.sizing``) — the size
  * ``protective_request`` (``tradex_strategy.core.brackets``) — the SL/TP legs

So the fix is sized to that: the bus is no longer an order-intake surface, and
the strategy's own two gates sit in front of the single remaining door.

The negative control below FAILS if the bus subscription is restored. That is
the point of it: a test that merely asserts today's behaviour cannot tell a
closed door from an open one.
"""

from __future__ import annotations

from datetime import UTC, datetime
from decimal import Decimal

import pytest
from tradex_domain import OHLC, Candle, OrderPlaced, OrderSide, Signal
from tradex_domain.enums import ExchangeId, OrderType, Timeframe
from tradex_domain.events import PlaceOrderCommand
from tradex_domain.execution import OrderRequest
from tradex_domain.instruments import Equity
from tradex_domain.value_objects import Price, Quantity
from tradex_execution.engine import ExecutionEngine
from tradex_execution.fill_sources import SimulatedFillSource
from tradex_reactive.bus import ReactiveBus
from tradex_strategy.core.brackets import STOP_KEY, TARGET_KEY
from tradex_strategy.core.engine import ReactiveStrategyEngine

INSTRUMENT = Equity.of(ExchangeId.NSE, "RELIANCE")


def _candle(close: str = "2500") -> Candle:
    price = Price(value=Decimal(close))
    return Candle(
        instrument=INSTRUMENT,
        timeframe=Timeframe.D1,
        ohlc=OHLC(open=price, high=price, low=price, close=price),
        volume=Quantity(value=Decimal("1000")),
        timestamp=datetime(2026, 8, 7, tzinfo=UTC),
    )


def _unsized_request() -> OrderRequest:
    """A hand-built request that BYPASSES the strategy's sizing gate.

    The quantity below is the tell: the shipped strategies all emit
    ``strength=1.0`` and leave sizing to ``PositionSizer``, which turns that
    into a real risk-sized number, not 1. An order of exactly one share
    arriving at the venue is the signature of a publisher that skipped the
    sizer.
    """
    return OrderRequest(
        instrument=INSTRUMENT,
        side=OrderSide.BUY,
        order_type=OrderType.MARKET,
        quantity=Quantity(value=Decimal("1")),
        price=Price(value=Decimal("2500")),
    )


class _AlwaysBuy:
    """Minimal strategy: one BUY signal on every bar, no quantity of its own."""

    def __init__(self) -> None:
        self.strategy_id = "ac22_bypass_probe"
        self.version = "1.0.0"

    def on_start(self, context) -> None:  # noqa: ANN001, ANN201
        """No-op start hook."""

    def on_stop(self, context) -> None:  # noqa: ANN001, ANN201
        """No-op stop hook."""

    def on_bar(self, context, bar) -> Signal:  # noqa: ANN001, ANN201
        return Signal(
            instrument=INSTRUMENT,
            direction=OrderSide.BUY,
            strength=1.0,
            reason="ac22_probe",
        )

    def on_quote(self, context, quote):  # noqa: ANN001, ANN201
        return None

    def on_fill(self, context, fill) -> None:  # noqa: ANN001, ANN201
        """No-op fill hook."""

    def on_event(self, event) -> None:  # noqa: ANN001, ANN201
        """No-op event hook."""


def _engine() -> tuple[ReactiveBus, ExecutionEngine, list[OrderPlaced]]:
    bus = ReactiveBus()
    engine = ExecutionEngine(bus, SimulatedFillSource())
    placed: list[OrderPlaced] = []
    bus.of_type(OrderPlaced).subscribe(placed.append)
    return bus, engine, placed


class TestTheBusIsNotAnOrderDoor:
    """The negative controls. Each fails if the bus door is re-opened."""

    def test_publishing_a_place_order_command_reaches_no_venue(self) -> None:
        """A ``PlaceOrderCommand`` on the bus is inert — the AC-22 hole.

        Re-adding ``bus.of_type(PlaceOrderCommand).subscribe(...)`` to
        ``ExecutionEngine._setup_pipeline`` makes this fail: the order appears
        in ``placed`` and the OMS.
        """
        bus, engine, placed = _engine()
        try:
            bus.publish(PlaceOrderCommand(request=_unsized_request()))

            assert placed == [], (
                "a PlaceOrderCommand published on the public bus reached the "
                f"venue: {[type(e).__name__ for e in placed]}. AC-22 requires "
                "submit() to be the only path."
            )
            assert engine.cache.all_orders() == [], (
                "the bus door created an order without submit()"
            )
        finally:
            engine.shutdown()

    def test_publishing_a_bare_order_request_reaches_no_venue(self) -> None:
        """A bare ``OrderRequest`` on the bus is inert too.

        The engine subscribed to this one as well. It had no legitimate
        publisher anywhere in the repository — every order already arrived via
        ``submit()`` or a strategy — so it was a door that existed only to be
        an opening. Re-adding ``bus.of_type(OrderRequest).subscribe(...)``
        makes this fail.
        """
        bus, engine, placed = _engine()
        try:
            bus.publish(_unsized_request())

            assert placed == [], (
                "a bare OrderRequest published on the public bus reached the "
                f"venue: {[type(e).__name__ for e in placed]}"
            )
            assert engine.cache.all_orders() == []
        finally:
            engine.shutdown()

    def test_a_strategy_cannot_publish_its_own_way_to_the_venue(self) -> None:
        """The whole point: a strategy has no order channel but ``submit``.

        A strategy that tries the old trick — grab the bus it was handed and
        publish an unsized order onto it — gets nothing. It is left with the
        sizing gate or nothing, which is the behaviour AC-22 asks for.
        """
        bus, engine, placed = _engine()
        strategy_engine = ReactiveStrategyEngine(bus, engine.submit)
        strategy = _AlwaysBuy()
        strategy_engine.register(strategy)
        try:
            # The cheat: the bus is right there, and it used to work.
            bus.publish(PlaceOrderCommand(request=_unsized_request()))

            assert placed == [], (
                "a strategy published straight to the venue, bypassing "
                "PositionSizer and protective_request entirely"
            )
            assert engine.cache.all_orders() == []
        finally:
            strategy_engine.dispose_all()
            engine.shutdown()

    def test_the_bus_does_not_carry_place_order_commands(self) -> None:
        """``PlaceOrderCommand`` is not on the order lane any more.

        Being off the lane matters independently of the subscription: a
        consumer watching the order lane can no longer be handed an
        order-intake message that looks like an order event.
        """
        from tradex_reactive.bus import _lane_for

        assert _lane_for(PlaceOrderCommand) == "default", (
            "PlaceOrderCommand is routable on a named lane again; it must fall "
            "to the default lane so it cannot masquerade as an order event"
        )


class TestSubmitIsTheOneDoor:
    """The positive control: the sanctioned path still works, and is gated."""

    def test_submit_still_reaches_the_venue(self) -> None:
        """Guards against 'fixing' AC-22 by simply breaking order flow."""
        bus, engine, placed = _engine()
        try:
            engine.submit(_unsized_request())

            assert len(placed) == 1
            assert engine.cache.all_orders()[0].quantity.value == Decimal("1")
        finally:
            engine.shutdown()

    def test_the_strategy_itself_is_still_sized_and_protected(self) -> None:
        """The sanctioned path keeps both strategy-layer gates.

        Sizing: the sizer's decision, not the signal's ``strength``, sets the
        quantity. Protection: a signal that declares levels gets them on the
        order. Both used to be skippable by publishing; neither is now.
        """
        bus = ReactiveBus()
        engine = ExecutionEngine(bus, SimulatedFillSource())
        submitted: list[OrderRequest] = []
        strategy_engine = ReactiveStrategyEngine(bus, submitted.append)
        strategy_engine.register(_AlwaysBuy())
        try:
            # Bar 1 defers; bar 2's open flushes it at 2500.
            bus.publish(_candle())
            bus.publish(_candle())

            assert len(submitted) == 1
            request = submitted[0]
            # Sized by the sizer, not `abs(strength) == 1`.
            assert request.quantity.value != Decimal("1"), (
                "the order quantity came from Signal.strength; sizing belongs "
                "to PositionSizer"
            )
            # A deterministic correlation id, so the order is auditable back
            # to the signal that produced it.
            assert request.correlation_id is not None
            assert request.tag == "ac22_bypass_probe@1.0.0"
        finally:
            strategy_engine.dispose_all()
            engine.shutdown()

    def test_declared_protective_levels_reach_the_order(self) -> None:
        """``protective_request`` still runs on the sanctioned path.

        The other half of what the bus door used to skip: a signal that
        declares a stop and a target must have both land on the order the
        engine receives, not be silently dropped.
        """
        bus = ReactiveBus()
        engine = ExecutionEngine(bus, SimulatedFillSource())
        submitted: list[OrderRequest] = []
        strategy_engine = ReactiveStrategyEngine(bus, submitted.append)

        class _WithLevels(_AlwaysBuy):
            def on_bar(self, context, bar) -> Signal:  # noqa: ANN001, ANN201
                return Signal(
                    instrument=INSTRUMENT,
                    direction=OrderSide.BUY,
                    strength=1.0,
                    reason="ac22_levels",
                    # The keys protective_request reads (STOP_KEY /
                    # TARGET_KEY), imported rather than retyped so this test
                    # cannot drift from the gate it is asserting on.
                    metadata={STOP_KEY: "2400", TARGET_KEY: "2600"},
                )

        strategy_engine.register(_WithLevels())
        try:
            bus.publish(_candle())
            bus.publish(_candle())

            assert len(submitted) == 1
            request = submitted[0]
            assert request.stop_loss_price is not None, (
                "the declared stop was dropped — protective_request did not run"
            )
            assert request.target_price is not None, (
                "the declared target was dropped — protective_request did not run"
            )
            assert request.stop_loss_price.value == Decimal("2400")
            assert request.target_price.value == Decimal("2600")
        finally:
            strategy_engine.dispose_all()
            engine.shutdown()


def test_no_source_ships_a_publisher_for_the_closed_door() -> None:
    """Nothing in the source tree publishes an order onto the bus.

    A repository-wide static check, so the door cannot be re-opened quietly by
    a new call site in some package the runtime suites do not import. Tests are
    excluded on purpose: several of them still *construct* a
    ``PlaceOrderCommand`` to prove it is inert.
    """
    import ast
    from pathlib import Path

    repo = Path(__file__).resolve().parents[3]
    # Directories that are not the shipped source tree. ``.kilo/`` holds a stale
    # copy of the repo (gitignored, on disk, with its own ``*/src`` trees), and
    # a glob over ``*/src`` reaches into it. A check that reads a dead copy of
    # the code is worse than no check: it fails on files nobody ships and says
    # nothing about the ones they do.
    _NOT_SOURCE = frozenset({".kilo", ".git", ".venv", "node_modules", "__pycache__"})
    offenders: list[str] = []
    for path in sorted(repo.rglob("*/src/**/*.py")):
        if any(part in _NOT_SOURCE for part in path.relative_to(repo).parts):
            continue
        try:
            tree = ast.parse(path.read_text(encoding="utf-8"))
        except (SyntaxError, UnicodeDecodeError):  # pragma: no cover
            continue
        for node in ast.walk(tree):
            if not isinstance(node, ast.Call):
                continue
            func = node.func
            name = func.attr if isinstance(func, ast.Attribute) else getattr(func, "id", "")
            if name == "publish":
                for arg in list(node.args) + [k.value for k in node.keywords]:
                    inner = arg
                    if isinstance(inner, ast.Call):
                        inner = inner.func
                    if isinstance(inner, ast.Name) and inner.id in {
                        "PlaceOrderCommand", "OrderRequest",
                    }:
                        offenders.append(f"{path.relative_to(repo)}:{node.lineno}")
    assert offenders == [], (
        f"source publishes an order onto the bus at {offenders}; AC-22 makes "
        "submit() the only path to the venue"
    )


@pytest.mark.parametrize(
    ("attr", "expected"),
    [
        # The engine keeps these as attributes for shutdown/diagnostics.
        ("_fill_disposable", True),
        # The two order-intake subscriptions are gone, not renamed.
        ("_pipeline_disposable", False),
        ("_command_disposable", False),
    ],
)
def test_only_the_fill_bridge_stays_subscribed(
    attr: str, expected: bool,
) -> None:
    """Structural check on the engine's subscriptions.

    The fill bridge (``OrderFilled``) is a *read* of the venue's truth and
    stays. The two order-intake subscriptions must not come back under any
    name: a renamed door is still a door.
    """
    bus, engine, _ = _engine()
    try:
        assert hasattr(engine, attr) is expected, (
            f"ExecutionEngine.{attr} presence changed; AC-22 removed the "
            "order-intake subscriptions and kept only the fill bridge"
        )
    finally:
        engine.shutdown()
