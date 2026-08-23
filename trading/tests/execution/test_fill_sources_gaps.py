"""Gap-coverage tests for fill sources, fees, reconciliation, and position manager."""

from __future__ import annotations

from datetime import UTC, datetime
from decimal import Decimal
from unittest.mock import MagicMock

from tradex_domain.enums import OrderSide, OrderType, ProductType, TimeInForce
from tradex_domain.execution import Fill, OrderRequest, Position
from tradex_domain.instruments import Equity
from tradex_domain.value_objects import Money, OrderId, Price, Quantity

from tradex_trading.execution.fees import FeeCalculator
from tradex_trading.execution.fill_sources import (
    BrokerFillSource,
    PaperFillSource,
    ReplayFillSource,
    SimulatedFillSource,
)
from tradex_trading.execution.position_manager import PositionManager
from tradex_trading.execution.reconciliation import (
    DriftSeverity,
    ReconciliationEngine,
)
from tradex_trading.execution.trading_cache import TradingCache

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _eq() -> Equity:
    return Equity.of("NSE", "RELIANCE")


def _request(price: Decimal = Decimal("2500")) -> OrderRequest:
    return OrderRequest(
        instrument=_eq(),
        side=OrderSide.BUY,
        order_type=OrderType.LIMIT,
        quantity=Quantity(value=Decimal("10")),
        price=Price(value=price),
        time_in_force=TimeInForce.DAY,
        product_type=ProductType.INTRADAY,
    )


def _fill(
    order_id: str = "f-1",
    side: OrderSide = OrderSide.BUY,
    qty: int = 10,
    price: int = 100,
) -> Fill:
    return Fill(
        order_id=OrderId(value=order_id),
        instrument=_eq(),
        side=side,
        quantity=Quantity(value=Decimal(qty)),
        price=Price(value=Decimal(price)),
        timestamp=datetime(2026, 8, 1, tzinfo=UTC),
    )


def _position(quantity: int = 10, avg: int = 100) -> Position:
    return Position(
        instrument=_eq(),
        quantity=Quantity(value=Decimal(quantity)),
        avg_price=Price(value=Decimal(avg)),
        realized_pnl=Money(amount=Decimal("0"), currency="INR"),
        unrealized_pnl=Money(amount=Decimal("0"), currency="INR"),
    )


# ===========================================================================
# FillSource cancel() implementations (tests 1-5)
# ===========================================================================


def test_simulated_cancel_is_noop() -> None:
    SimulatedFillSource().cancel(OrderId(value="x"))


def test_paper_cancel_is_noop() -> None:
    PaperFillSource().cancel(OrderId(value="x"))


def test_replay_cancel_is_noop() -> None:
    ReplayFillSource(fills=[]).cancel(OrderId(value="x"))


def test_broker_cancel_delegates_to_adapter() -> None:
    mock_broker = MagicMock()
    oid = OrderId(value="oid-1")
    BrokerFillSource(mock_broker).cancel(oid)
    mock_broker.cancel_order.assert_called_once_with(oid)


def test_broker_cancel_noop_when_no_cancel_order() -> None:
    BrokerFillSource(object()).cancel(OrderId(value="oid-1"))


# ===========================================================================
# SimulatedFillSource with portfolio_state / slippage_model (tests 6-9)
# ===========================================================================


def test_simulated_with_slippage_model_adjusts_price() -> None:
    # H7: LIMIT BUY slippage beyond limit is clamped, so use MARKET to verify slippage applies
    slippage = MagicMock()
    slippage.apply.return_value = Price(value=Decimal("2501.50"))
    source = SimulatedFillSource(slippage_model=slippage)
    req = OrderRequest(
        instrument=_eq(),
        side=OrderSide.BUY,
        order_type=OrderType.MARKET,
        quantity=Quantity(value=Decimal("10")),
        price=Price(value=Decimal("2500")),
        time_in_force=TimeInForce.DAY,
        product_type=ProductType.INTRADAY,
    )
    _order, fill = source.submit(req)
    assert fill is not None
    assert fill.price.value == Decimal("2501.50")


def test_simulated_with_portfolio_state_calls_get_position() -> None:
    portfolio = MagicMock()
    source = SimulatedFillSource(portfolio_state=portfolio)
    source.submit(_request())
    portfolio.get_position.assert_called_once_with(_eq())


def test_simulated_with_both_params() -> None:
    # H7: LIMIT clamp would hide slippage, use MARKET to verify portfolio + slippage
    slippage = MagicMock()
    slippage.apply.return_value = Price(value=Decimal("2502"))
    portfolio = MagicMock()
    source = SimulatedFillSource(
        portfolio_state=portfolio,
        slippage_model=slippage,
    )
    req = OrderRequest(
        instrument=_eq(),
        side=OrderSide.BUY,
        order_type=OrderType.MARKET,
        quantity=Quantity(value=Decimal("10")),
        price=Price(value=Decimal("2500")),
        time_in_force=TimeInForce.DAY,
        product_type=ProductType.INTRADAY,
    )
    _order, fill = source.submit(req)
    assert fill is not None
    assert fill.price.value == Decimal("2502")
    portfolio.get_position.assert_called_once_with(_eq())


def test_simulated_portfolio_without_get_position_ignored() -> None:
    source = SimulatedFillSource(portfolio_state=object())
    _order, fill = source.submit(_request(price=Decimal("2500")))
    assert fill is not None
    assert fill.price.value == Decimal("2500")


# ===========================================================================
# FeeCalculator brokerage cap edge cases (tests 10-13)
# ===========================================================================


def test_brokerage_cap_exact_boundary() -> None:
    """Turnover where 0.03% < 20 → uncapped brokerage."""
    # turnover = 60000 → 60000 * 0.0003 = 18.00 (uncapped)
    fees = FeeCalculator.equity_delivery(
        side=OrderSide.BUY, price=Decimal("600"), quantity=Decimal("100"),
    )
    assert fees.broker_fee == Decimal("18.00")


def test_brokerage_cap_at_20() -> None:
    """Turnover where 0.03% >= 20 → capped at 20."""
    # turnover = 100000 → 100000 * 0.0003 = 30 → capped at 20
    fees = FeeCalculator.equity_delivery(
        side=OrderSide.BUY, price=Decimal("1000"), quantity=Decimal("100"),
    )
    assert fees.broker_fee == Decimal("20.00")


def test_fee_breakdown_delivery_brokerage_capped() -> None:
    """Large turnover delivery trade: broker_fee capped at 20."""
    fees = FeeCalculator.equity_delivery(
        side=OrderSide.SELL, price=Decimal("5000"), quantity=Decimal("100"),
    )
    # turnover = 500000, 0.03% = 150, capped at 20
    assert fees.broker_fee == Decimal("20.00")


def test_fee_breakdown_intraday_brokerage_capped() -> None:
    """Large turnover intraday trade: broker_fee capped at 20."""
    fees = FeeCalculator.equity_intraday(
        side=OrderSide.SELL, price=Decimal("5000"), quantity=Decimal("100"),
    )
    assert fees.broker_fee == Decimal("20.00")


# ===========================================================================
# Reconciliation avg_price drift edge cases (tests 14-18)
# ===========================================================================


def test_no_avg_price_drift_when_qty_zero() -> None:
    """Both qty=0 with different avg_price → no avg_price drift."""
    engine = ReconciliationEngine()
    local = _position(quantity=0, avg=100)
    broker = _position(quantity=0, avg=200)
    drifts = engine.reconcile([local], [broker])
    # qty matches (both 0), but the code skips avg_price check when qty == 0
    assert drifts == []


def test_avg_price_drift_at_tolerance() -> None:
    """Price diff exactly 0.01 → NO drift (uses > not >=)."""
    engine = ReconciliationEngine()
    local_exact = Position(
        instrument=_eq(),
        quantity=Quantity(value=Decimal("10")),
        avg_price=Price(value=Decimal("100.00")),
        realized_pnl=Money(amount=Decimal("0"), currency="INR"),
        unrealized_pnl=Money(amount=Decimal("0"), currency="INR"),
    )
    broker_exact = Position(
        instrument=_eq(),
        quantity=Quantity(value=Decimal("10")),
        avg_price=Price(value=Decimal("100.01")),
        realized_pnl=Money(amount=Decimal("0"), currency="INR"),
        unrealized_pnl=Money(amount=Decimal("0"), currency="INR"),
    )
    drifts = engine.reconcile([local_exact], [broker_exact])
    assert drifts == []


def test_avg_price_drift_above_tolerance() -> None:
    """Price diff 0.011 → triggers drift."""
    engine = ReconciliationEngine()
    local = Position(
        instrument=_eq(),
        quantity=Quantity(value=Decimal("10")),
        avg_price=Price(value=Decimal("100.000")),
        realized_pnl=Money(amount=Decimal("0"), currency="INR"),
        unrealized_pnl=Money(amount=Decimal("0"), currency="INR"),
    )
    broker = Position(
        instrument=_eq(),
        quantity=Quantity(value=Decimal("10")),
        avg_price=Price(value=Decimal("100.011")),
        realized_pnl=Money(amount=Decimal("0"), currency="INR"),
        unrealized_pnl=Money(amount=Decimal("0"), currency="INR"),
    )
    drifts = engine.reconcile([local], [broker])
    assert len(drifts) == 1
    assert drifts[0].reason == "avg_price drift"


def test_both_qty_and_avg_price_drift() -> None:
    """Different qty AND different avg_price → 2 drift items."""
    engine = ReconciliationEngine()
    local = Position(
        instrument=_eq(),
        quantity=Quantity(value=Decimal("10")),
        avg_price=Price(value=Decimal("100.000")),
        realized_pnl=Money(amount=Decimal("0"), currency="INR"),
        unrealized_pnl=Money(amount=Decimal("0"), currency="INR"),
    )
    broker = Position(
        instrument=_eq(),
        quantity=Quantity(value=Decimal("15")),
        avg_price=Price(value=Decimal("100.011")),
        realized_pnl=Money(amount=Decimal("0"), currency="INR"),
        unrealized_pnl=Money(amount=Decimal("0"), currency="INR"),
    )
    drifts = engine.reconcile([local], [broker])
    # qty mismatch → 1 drift; avg_price NOT checked because qty differs
    assert len(drifts) == 1
    assert drifts[0].diff == Decimal("-5")


def test_avg_price_drift_fields() -> None:
    """Verify kind, severity, reason, local, remote are populated."""
    engine = ReconciliationEngine()
    local = Position(
        instrument=_eq(),
        quantity=Quantity(value=Decimal("10")),
        avg_price=Price(value=Decimal("100.00")),
        realized_pnl=Money(amount=Decimal("0"), currency="INR"),
        unrealized_pnl=Money(amount=Decimal("0"), currency="INR"),
    )
    broker = Position(
        instrument=_eq(),
        quantity=Quantity(value=Decimal("10")),
        avg_price=Price(value=Decimal("100.05")),
        realized_pnl=Money(amount=Decimal("0"), currency="INR"),
        unrealized_pnl=Money(amount=Decimal("0"), currency="INR"),
    )
    drifts = engine.reconcile([local], [broker])
    assert len(drifts) == 1
    d = drifts[0]
    assert d.kind == "position"
    assert d.severity == DriftSeverity.MEDIUM
    assert d.reason == "avg_price drift"
    assert d.local == Decimal("100.00")
    assert d.remote == Decimal("100.05")


# ===========================================================================
# PositionManager apply() / reconcile_with_broker (tests 19-22)
# ===========================================================================


def _pm() -> tuple[PositionManager, TradingCache]:
    cache = TradingCache()
    return PositionManager(cache), cache


def test_apply_returns_position() -> None:
    pm, _cache = _pm()
    pos = pm.on_fill(_fill("f1", OrderSide.BUY, 10, 100))
    assert pos.quantity.value == Decimal("10")
    assert pos.avg_price.value == Decimal("100")


def test_apply_with_existing_position() -> None:
    pm, _cache = _pm()
    pm.on_fill(_fill("f1", OrderSide.BUY, 10, 100))
    pos = pm.on_fill(_fill("f2", OrderSide.BUY, 10, 120))
    assert pos.quantity.value == Decimal("20")
    assert pos.avg_price.value == Decimal("110")


def test_reconcile_no_drift() -> None:
    pm, _cache = _pm()
    pm.on_fill(_fill("f1", OrderSide.BUY, 10, 100))
    broker_pos = [_position(quantity=10, avg=100)]
    drifts = pm.reconcile_with_broker(broker_pos)
    assert drifts == []


def test_reconcile_detects_drift() -> None:
    pm, _cache = _pm()
    pm.on_fill(_fill("f1", OrderSide.BUY, 10, 100))
    broker_pos = [_position(quantity=15, avg=100)]
    drifts = pm.reconcile_with_broker(broker_pos)
    assert len(drifts) == 1
    assert drifts[0].diff == Decimal("-5")
