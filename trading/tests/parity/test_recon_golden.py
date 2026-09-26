"""Golden reconciliation drift gate (Wave C2).

Recorded broker/local snapshots must produce stable drift keys and severities
from ``ReconciliationEngine`` / ``ExecutionEngine.reconcile``. Startup policy
trips the kill switch on HIGH/CRITICAL items (see reconciliation-drift runbook).
"""

from __future__ import annotations

import json
from decimal import Decimal
from pathlib import Path
from typing import Any

import pytest
from tradex_domain import Equity, Money, Position, Price, Quantity
from tradex_domain.enums import OrderSide, OrderStatus, OrderType, ProductType, TimeInForce
from tradex_domain.execution import Order
from tradex_domain.value_objects import OrderId

from tradex_trading.execution.engine import ExecutionEngine
from tradex_trading.execution.fill_sources import SimulatedFillSource
from tradex_trading.execution.reconciliation import (
    DriftItem,
    DriftSeverity,
    ReconciliationEngine,
)
from tradex_trading.execution.trading_cache import TradingCache
from tradex_trading.reactive.bus import ReactiveBus

_GOLDEN = Path(__file__).resolve().parent / "goldens" / "recon_startup_drift.json"


def _load_golden() -> dict[str, Any]:
    return json.loads(_GOLDEN.read_text(encoding="utf-8"))


def _equity(exchange: str, symbol: str) -> Equity:
    return Equity.of(exchange, symbol)


def _position(row: dict[str, Any]) -> Position:
    inst = _equity(row["exchange"], row["symbol"])
    return Position(
        instrument=inst,
        quantity=Quantity(value=Decimal(str(row["quantity"]))),
        avg_price=Price(value=Decimal(str(row["avg_price"]))),
        realized_pnl=Money(amount=Decimal("0"), currency="INR"),
        unrealized_pnl=Money(amount=Decimal("0"), currency="INR"),
    )


def _order(row: dict[str, Any]) -> Order:
    inst = _equity(row["exchange"], row["symbol"])
    return Order(
        order_id=OrderId(value=row["order_id"]),
        instrument=inst,
        side=OrderSide.BUY,
        order_type=OrderType.LIMIT,
        quantity=Quantity(value=Decimal(str(row["quantity"]))),
        price=Price(value=Decimal(str(row["price"]))),
        time_in_force=TimeInForce.DAY,
        status=OrderStatus(row["status"]),
        filled_quantity=Quantity(value=Decimal(str(row["filled"]))),
        product_type=ProductType.INTRADAY,
    )


def _drift_key(item: DriftItem) -> tuple[str, str, str, str]:
    kind = item.kind or "position"
    key = item.key or item.symbol
    severity = item.severity.value if isinstance(item.severity, DriftSeverity) else str(item.severity)
    return (kind, key, severity, item.reason or "")


def _expected_key(row: dict[str, Any]) -> tuple[str, str, str, str]:
    return (row["kind"], row["key"], row["severity"], row.get("reason", ""))


def _collect_drifts(
    *,
    local_positions: list[dict[str, Any]],
    broker_positions: list[dict[str, Any]],
    local_orders: list[dict[str, Any]],
    broker_orders: list[dict[str, Any]],
) -> list[DriftItem]:
    engine = ReconciliationEngine()
    drifts: list[DriftItem] = []
    lp = [_position(r) for r in local_positions]
    bp = [_position(r) for r in broker_positions]
    lo = [_order(r) for r in local_orders]
    bo = [_order(r) for r in broker_orders]
    if lp or bp:
        drifts.extend(engine.reconcile(lp, bp))
    if lo or bo:
        drifts.extend(engine.compare_orders(lo, bo))
    return drifts


def _collect_engine_drifts(
    *,
    local_positions: list[dict[str, Any]],
    broker_positions: list[dict[str, Any]],
    local_orders: list[dict[str, Any]],
    broker_orders: list[dict[str, Any]],
) -> list[DriftItem]:
    cache = TradingCache()
    for row in local_orders:
        cache.update_order(_order(row))
    for row in local_positions:
        cache.update_position(_position(row))
    bus = ReactiveBus()
    exec_engine = ExecutionEngine(
        bus=bus,
        fill_source=SimulatedFillSource(),
        cache=cache,
    )
    kwargs: dict[str, Any] = {}
    if local_positions or broker_positions:
        kwargs["local_positions"] = [_position(r) for r in local_positions]
        kwargs["broker_positions"] = [_position(r) for r in broker_positions]
    if local_orders or broker_orders:
        kwargs["broker_orders"] = [_order(r) for r in broker_orders]
    return exec_engine.reconcile(**kwargs)


def _startup_blocks(drifts: list[DriftItem], policy: dict[str, Any]) -> bool:
    blocking = {DriftSeverity(s) for s in policy["trip_on_severities"]}
    return any(d.severity in blocking for d in drifts)


@pytest.fixture(scope="module")
def golden() -> dict[str, Any]:
    return _load_golden()


@pytest.mark.parametrize("scenario", _load_golden()["scenarios"], ids=lambda s: s["name"])
def test_golden_reconciliation_engine_drifts(scenario: dict[str, Any]) -> None:
    drifts = _collect_drifts(
        local_positions=scenario.get("local_positions", []),
        broker_positions=scenario.get("broker_positions", []),
        local_orders=scenario.get("local_orders", []),
        broker_orders=scenario.get("broker_orders", []),
    )
    expected = {_expected_key(row) for row in scenario["expected_drift_keys"]}
    actual = {_drift_key(d) for d in drifts}
    assert actual == expected, f"drift keys mismatch: extra={actual - expected} missing={expected - actual}"


@pytest.mark.parametrize("scenario", _load_golden()["scenarios"], ids=lambda s: s["name"])
def test_execution_engine_reconcile_matches_golden(scenario: dict[str, Any]) -> None:
    drifts = _collect_engine_drifts(
        local_positions=scenario.get("local_positions", []),
        broker_positions=scenario.get("broker_positions", []),
        local_orders=scenario.get("local_orders", []),
        broker_orders=scenario.get("broker_orders", []),
    )
    expected = {_expected_key(row) for row in scenario["expected_drift_keys"]}
    actual = {_drift_key(d) for d in drifts}
    assert actual == expected


@pytest.mark.parametrize("scenario", _load_golden()["scenarios"], ids=lambda s: s["name"])
def test_kill_switch_policy_matches_expectation(
    scenario: dict[str, Any], golden: dict[str, Any]
) -> None:
    drifts = _collect_drifts(
        local_positions=scenario.get("local_positions", []),
        broker_positions=scenario.get("broker_positions", []),
        local_orders=scenario.get("local_orders", []),
        broker_orders=scenario.get("broker_orders", []),
    )
    policy = golden["kill_switch_policy"]
    blocks = _startup_blocks(drifts, policy)
    assert blocks is scenario["expect_kill_trip"]


def test_critical_severity_trips_kill_switch(golden: dict[str, Any]) -> None:
    """Document CRITICAL handling even though the engine does not emit it yet."""
    row = golden["critical_kill_trip_fixture"]["drift"]
    item = DriftItem(
        kind=row["kind"],
        key=row["key"],
        severity=DriftSeverity(row["severity"]),
        reason=row["reason"],
    )
    policy = golden["kill_switch_policy"]
    assert _startup_blocks([item], policy) is golden["critical_kill_trip_fixture"]["expect_kill_trip"]

    bus = ReactiveBus()
    engine = ExecutionEngine(bus=bus, fill_source=SimulatedFillSource())
    assert engine.kill_switch is False
    engine.trip_kill_switch(reason=policy["trip_reason"])
    assert engine.kill_switch is True
