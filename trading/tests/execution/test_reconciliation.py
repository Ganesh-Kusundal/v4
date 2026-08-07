"""WS-E contract tests: reconciliation engine — ported from v3.

REWRITTEN for v4 API: ReconciliationEngine.reconcile(local, broker)
→ list[DriftItem].  v3 had DriftSeverity, compare_orders(),
compare_funds() — none exist in v4.  v4 only does position-level
reconciliation.
"""

from __future__ import annotations

from decimal import Decimal

from tradex_domain import (
    Equity,
    Money,
    Position,
    Price,
    Quantity,
)

from tradex_trading.execution.reconciliation import (
    ReconciliationEngine,
)


def _eq() -> Equity:
    return Equity.of("NSE", "RELIANCE")


def _position(quantity: int = 10, avg: int = 100) -> Position:
    return Position(
        instrument=_eq(),
        quantity=Quantity(value=Decimal(quantity)),
        avg_price=Price(value=Decimal(avg)),
        realized_pnl=Money(amount=Decimal("0"), currency="INR"),
        unrealized_pnl=Money(amount=Decimal("0"), currency="INR"),
    )


def test_identical_positions_produce_no_drift() -> None:
    engine = ReconciliationEngine()
    assert engine.reconcile([_position()], [_position()]) == []


def test_quantity_mismatch_produces_drift() -> None:
    engine = ReconciliationEngine()
    drifts = engine.reconcile(
        [_position(quantity=10)],
        [_position(quantity=15)],
    )
    assert len(drifts) == 1
    assert drifts[0].symbol == "RELIANCE"
    assert drifts[0].local_quantity == Decimal("10")
    assert drifts[0].broker_quantity == Decimal("15")
    assert drifts[0].diff == Decimal("-5")


def test_missing_broker_position_is_drift() -> None:
    engine = ReconciliationEngine()
    drifts = engine.reconcile([_position()], [])
    assert len(drifts) == 1
    assert drifts[0].broker_quantity == Decimal("0")
    assert drifts[0].diff == Decimal("10")


def test_missing_local_position_is_drift() -> None:
    engine = ReconciliationEngine()
    drifts = engine.reconcile([], [_position()])
    assert len(drifts) == 1
    assert drifts[0].local_quantity == Decimal("0")
    assert drifts[0].diff == Decimal("-10")


def test_empty_inputs_produce_no_drift() -> None:
    engine = ReconciliationEngine()
    assert engine.reconcile([], []) == []


def test_multiple_symbols_reconciled_independently() -> None:
    engine = ReconciliationEngine()
    tcs = Equity.of("NSE", "TCS")
    tcs_pos = Position(
        instrument=tcs,
        quantity=Quantity(value=Decimal("5")),
        avg_price=Price(value=Decimal("3000")),
        realized_pnl=Money(amount=Decimal("0"), currency="INR"),
        unrealized_pnl=Money(amount=Decimal("0"), currency="INR"),
    )
    drifts = engine.reconcile(
        [_position(quantity=10), tcs_pos],
        [_position(quantity=10)],
    )
    # TCS is missing from broker → drift
    assert len(drifts) == 1
    assert drifts[0].symbol == "TCS"
