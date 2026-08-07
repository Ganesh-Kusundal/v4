"""Tests for v3-ported reconciliation symbols: DriftSeverity, compare_orders, compare_funds."""

from __future__ import annotations

from decimal import Decimal

from tradex_domain.enums import OrderSide, OrderStatus, OrderType, ProductType, TimeInForce
from tradex_domain.execution import Account, Order
from tradex_domain.instruments import Equity
from tradex_domain.value_objects import AccountId, Money, OrderId, Price, Quantity

from tradex_trading.execution.reconciliation import (
    DriftSeverity,
    ReconciliationEngine,
)

# ---------------------------------------------------------------------------
# DriftSeverity
# ---------------------------------------------------------------------------


class TestDriftSeverity:
    """DriftSeverity StrEnum."""

    def test_values(self) -> None:
        assert DriftSeverity.LOW == "LOW"
        assert DriftSeverity.MEDIUM == "MEDIUM"
        assert DriftSeverity.HIGH == "HIGH"
        assert DriftSeverity.CRITICAL == "CRITICAL"

    def test_is_str(self) -> None:
        assert isinstance(DriftSeverity.LOW, str)


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _make_order(
    order_id: str = "ord-1",
    quantity: int = 10,
    filled: int = 0,
    price: int = 100,
    status: OrderStatus = OrderStatus.FILLED,
) -> Order:
    return Order(
        order_id=OrderId(value=order_id),
        instrument=Equity.of("NSE", "RELIANCE"),
        side=OrderSide.BUY,
        order_type=OrderType.LIMIT,
        quantity=Quantity(value=Decimal(quantity)),
        price=Price(value=Decimal(price)),
        time_in_force=TimeInForce.DAY,
        status=status,
        filled_quantity=Quantity(value=Decimal(filled)),
        product_type=ProductType.INTRADAY,
    )


def _make_account(
    balance: str = "10000",
    equity: str = "50000",
) -> Account:
    return Account(
        account_id=AccountId(value="ACC-1"),
        balance=Money(amount=Decimal(balance), currency="INR"),
        margin=Money(amount=Decimal("5000"), currency="INR"),
        equity=Money(amount=Decimal(equity), currency="INR"),
    )


# ---------------------------------------------------------------------------
# compare_orders
# ---------------------------------------------------------------------------


class TestCompareOrders:
    """ReconciliationEngine.compare_orders."""

    def test_identical_orders_no_drift(self) -> None:
        engine = ReconciliationEngine()
        local = [_make_order("a")]
        broker = [_make_order("a")]
        assert engine.compare_orders(local, broker) == []

    def test_missing_local_order(self) -> None:
        engine = ReconciliationEngine()
        drifts = engine.compare_orders([], [_make_order("x")])
        assert len(drifts) == 1
        assert drifts[0].kind == "order"
        assert drifts[0].severity == DriftSeverity.HIGH
        assert drifts[0].reason == "missing local order"

    def test_missing_broker_order(self) -> None:
        engine = ReconciliationEngine()
        drifts = engine.compare_orders([_make_order("x")], [])
        assert len(drifts) == 1
        assert drifts[0].reason == "missing broker order"

    def test_quantity_mismatch(self) -> None:
        engine = ReconciliationEngine()
        local = [_make_order("a", quantity=10)]
        broker = [_make_order("a", quantity=20)]
        drifts = engine.compare_orders(local, broker)
        assert len(drifts) == 1
        assert drifts[0].severity == DriftSeverity.HIGH
        assert drifts[0].reason == "quantity mismatch"

    def test_filled_quantity_mismatch(self) -> None:
        engine = ReconciliationEngine()
        local = [_make_order("a", filled=5)]
        broker = [_make_order("a", filled=10)]
        drifts = engine.compare_orders(local, broker)
        assert len(drifts) == 1
        assert drifts[0].reason == "filled_quantity mismatch"

    def test_price_drift(self) -> None:
        engine = ReconciliationEngine()
        local = [_make_order("a", price=100)]
        broker = [_make_order("a", price=200)]
        drifts = engine.compare_orders(local, broker)
        assert any(d.reason == "price drift" for d in drifts)

    def test_status_lag(self) -> None:
        engine = ReconciliationEngine()
        local = [_make_order("a", status=OrderStatus.ACK)]
        broker = [_make_order("a", status=OrderStatus.FILLED)]
        drifts = engine.compare_orders(local, broker)
        assert any(d.reason == "status lag" for d in drifts)


# ---------------------------------------------------------------------------
# compare_funds
# ---------------------------------------------------------------------------


class TestCompareFunds:
    """ReconciliationEngine.compare_funds."""

    def test_matching_accounts_no_drift(self) -> None:
        engine = ReconciliationEngine()
        acc = _make_account()
        assert engine.compare_funds(acc, acc) == []

    def test_balance_mismatch_high_severity(self) -> None:
        engine = ReconciliationEngine()
        local = _make_account(balance="10000")
        broker = _make_account(balance="5000")
        drifts = engine.compare_funds(local, broker)
        assert len(drifts) == 1
        assert drifts[0].kind == "funds"
        assert drifts[0].severity == DriftSeverity.HIGH
        assert drifts[0].reason == "balance mismatch"

    def test_equity_drift_medium_severity(self) -> None:
        engine = ReconciliationEngine()
        local = _make_account(equity="50000")
        broker = _make_account(equity="40000")
        drifts = engine.compare_funds(local, broker)
        assert len(drifts) == 1
        assert drifts[0].severity == DriftSeverity.MEDIUM
        assert drifts[0].reason == "equity drift"

    def test_within_tolerance_no_drift(self) -> None:
        engine = ReconciliationEngine()
        local = _make_account(balance="10000.005")
        broker = _make_account(balance="10000.00")
        drifts = engine.compare_funds(local, broker)
        assert drifts == []
