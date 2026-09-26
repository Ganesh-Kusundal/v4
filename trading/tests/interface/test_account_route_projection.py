"""The account route must derive its numbers from the canonical projection.

``chart.py`` re-implemented the position projection inline, converting Decimals
to floats and defaulting a missing P&L to ``0.0``. A float round-trip and a
silent zero are exactly the two ways an API can disagree with the numbers the
parity suite pins. The wire shape stays (the chart types these as numbers), but
the values come from ``execution_projection`` like every other consumer.
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


def _engine_with_position() -> ExecutionEngine:
    bus = ReactiveBus()
    engine = ExecutionEngine(bus, SimulatedFillSource(), cash=Decimal("100000"))
    bus.publish(
        PlaceOrderCommand(
            request=OrderRequest(
                instrument=INSTRUMENT,
                side=OrderSide.BUY,
                order_type=OrderType.LIMIT,
                # 100.10: canonical normalizes to "100.1", a float renders
                # "100.10". A value float preserves could not tell the paths apart.
                quantity=Quantity(value=Decimal("3")),
                price=Price(value=Decimal("100.10")),
                time_in_force=TimeInForce.DAY,
            ),
        ),
    )
    return engine


class _StubSession:
    def __init__(self, engine, account) -> None:
        self.engine = engine
        self.broker = type("B", (), {"get_account": staticmethod(lambda: account)})()


def _account():
    from tradex_domain.execution import Account
    from tradex_domain.value_objects import AccountId, Money

    return Account(
        account_id=AccountId("acct-1"),
        balance=Money(amount=Decimal("100000")),
        margin=Money(amount=Decimal("50000")),
    )


def test_account_route_sources_its_numbers_from_the_projection() -> None:
    """The route must call the canonical projection, not re-derive the values."""
    pytest.importorskip("fastapi")
    from fastapi.testclient import TestClient
    from tradex_interfaces.fastapi_app import create_app
    from tradex_interfaces.routes import chart as chart_route

    engine = _engine_with_position()
    calls: list[int] = []
    # Patch where the name is USED, not where it is defined: the route imported
    # the symbol directly, so rebinding the source module would prove nothing.
    original = chart_route.execution_projection

    def counting(*args, **kwargs):
        calls.append(1)
        return original(*args, **kwargs)

    chart_route.execution_projection = counting
    try:
        response = TestClient(
            create_app(session=_StubSession(engine, _account())),
        ).get("/api/charts/account")
    finally:
        chart_route.execution_projection = original
        engine.shutdown()

    assert response.status_code == 200
    assert calls, (
        "the account route served its numbers without calling the canonical "
        "projection — the agreement is coincidental, not enforced"
    )


def test_account_route_matches_the_canonical_values() -> None:
    """Decimal precision must survive the float boundary intact."""
    pytest.importorskip("fastapi")
    from fastapi.testclient import TestClient
    from tradex_interfaces.fastapi_app import create_app

    engine = _engine_with_position()
    try:
        canonical = {
            row["instrument"]: row
            for row in execution_projection(
                [], engine.cache.all_positions(),
            )["positions"]
        }
        body = TestClient(
            create_app(session=_StubSession(engine, _account())),
        ).get("/api/charts/account").json()
    finally:
        engine.shutdown()

    served = body["positions"][0]
    expected = canonical[str(INSTRUMENT.instrument_id)]
    # 100.10 canonicalizes to "100.1"; a float renders 100.1 too, so compare
    # against the canonical decimal rather than a re-derived value.
    assert Decimal(str(served["avg_price"])) == Decimal(expected["avg_price"])
    assert Decimal(str(served["net_qty"])) == Decimal(expected["quantity"])


def test_a_missing_margin_no_longer_crashes_the_route() -> None:
    """A broker that reports no margin must degrade, not 500."""
    pytest.importorskip("fastapi")
    from fastapi.testclient import TestClient
    from tradex_domain.execution import Account
    from tradex_domain.value_objects import AccountId, Money
    from tradex_interfaces.fastapi_app import create_app

    engine = _engine_with_position()
    no_margin = Account(
        account_id=AccountId("acct-1"), balance=Money(amount=Decimal("1000")),
    )
    try:
        response = TestClient(
            create_app(session=_StubSession(engine, no_margin)),
        ).get("/api/charts/account")
    finally:
        engine.shutdown()

    assert response.status_code == 200
    assert response.json()["margin"] == "0"
