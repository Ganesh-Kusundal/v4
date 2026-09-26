"""The canonical projection must be a real API surface, not test-only code.

``execution_projection`` was the parity contract between execution modes but had
no production caller, so nothing outside the test suite could observe it. These
tests pin that the API serves the same canonical numbers the parity suite
compares — Decimal values as exact strings, never floats.
"""

from __future__ import annotations

from decimal import Decimal

import pytest
from tradex_domain.enums import OrderSide, OrderType, TimeInForce
from tradex_domain.events import PlaceOrderCommand
from tradex_domain.execution import OrderRequest
from tradex_domain.instruments import Equity
from tradex_domain.value_objects import Price, Quantity
from tradex_execution.engine import ExecutionEngine
from tradex_execution.fill_sources import SimulatedFillSource
from tradex_execution.projection import execution_projection
from tradex_reactive.bus import ReactiveBus

INSTRUMENT = Equity.of("NSE", "RELIANCE")


def _bus_with_fills(cash: str = "100000") -> tuple[ReactiveBus, ExecutionEngine, list]:
    bus = ReactiveBus()
    engine = ExecutionEngine(bus, SimulatedFillSource(), cash=Decimal(cash))
    from tradex_domain.events import OrderFilled

    fills: list = []
    bus.of_type(OrderFilled).subscribe(lambda e: fills.append(e.fill))
    bus.publish(
        PlaceOrderCommand(
            request=OrderRequest(
                instrument=INSTRUMENT,
                side=OrderSide.BUY,
                order_type=OrderType.LIMIT,
                quantity=Quantity(value=Decimal("3")),
                # 100.10 is deliberate: the canonical projection normalizes it
                # to "100.1", while a float round-trip renders "100.10". A price
                # that survives float unchanged could not tell the two paths
                # apart, and the test would pass for the wrong reason.
                price=Price(value=Decimal("100.10")),
                time_in_force=TimeInForce.DAY,
            ),
        ),
    )
    return bus, engine, fills


def test_the_api_route_actually_calls_the_canonical_projection() -> None:
    """The endpoint must be wired to the projection, not merely agree with it.

    Comparing the route's output to the projection proves they agree today, but
    not that the route *uses* it — an independent reimplementation would also
    agree. Patching the projection and watching the response change is what
    proves the dependency is real.
    """
    pytest.importorskip("fastapi")
    import tradex_execution.projection as projection_module
    from fastapi.testclient import TestClient
    from tradex_interfaces.fastapi_app import create_app
    from tradex_interfaces.routes import portfolio as portfolio_route

    _bus, engine, fills = _bus_with_fills()
    calls: list[int] = []
    original = projection_module.execution_projection

    def counting(*args, **kwargs):
        calls.append(1)
        return original(*args, **kwargs)

    portfolio_route.execution_projection = counting
    try:
        app = create_app()
        app.state.session = _StubSession(engine)
        response = TestClient(app).get("/positions")
    finally:
        portfolio_route.execution_projection = original
        engine.shutdown()

    assert response.status_code == 200
    assert calls, (
        "the /positions route served its numbers without calling the canonical "
        "projection — the agreement is coincidental, not enforced"
    )


def test_projection_keeps_decimal_exactness() -> None:
    """Decimals must survive as exact strings, not rounded through float."""
    _bus, engine, fills = _bus_with_fills()
    try:
        projection = execution_projection(
            fills, engine.cache.all_positions(), engine.cash_snapshot().cash,
        )
    finally:
        engine.shutdown()

    assert projection["fills"][0]["price"] == "100.1", (
        "a float path would render 100.10 here"
    )
    assert projection["fills"][0]["quantity"] == "3"
    # 100000 opening - 3 x 100.10 = 99699.70 (no fee calculator bound here).
    assert projection["cash"] == "99699.7"
    assert projection["positions"][0]["avg_price"] == "100.1"


def test_positions_endpoint_serves_the_canonical_projection() -> None:
    """The API must expose the same numbers the parity suite compares."""
    pytest.importorskip("fastapi")
    from fastapi.testclient import TestClient
    from tradex_interfaces.fastapi_app import create_app

    _bus, engine, fills = _bus_with_fills()
    try:
        canonical = execution_projection(
            fills, engine.cache.all_positions(), engine.cash_snapshot().cash,
        )
        app = create_app()
        app.state.session = _StubSession(engine)
        response = TestClient(app).get("/positions")
        assert response.status_code == 200
        rows = response.json()
    finally:
        engine.shutdown()

    assert rows, "the ladder should have produced a position"
    by_instrument = {row["instrument"]: row for row in rows}
    for expected in canonical["positions"]:
        served = by_instrument[expected["instrument"]]
        assert served["avg_price"] == expected["avg_price"], (
            "the API must serve the canonical avg_price, not a float rendering"
        )
        assert served["realized_pnl"] == expected["realized_pnl"]
        assert served["unrealized_pnl"] == expected["unrealized_pnl"]


class _StubSession:
    """The minimum a portfolio route reads: an engine with a cache."""

    def __init__(self, engine) -> None:
        self.engine = engine
        self.broker = None
