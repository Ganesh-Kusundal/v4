"""Gap tests for fees — PricingService.total_cost and STT delivery rates."""

from __future__ import annotations

from datetime import UTC, datetime
from decimal import Decimal

from tradex_domain.enums import OrderSide
from tradex_domain.execution import Fill
from tradex_domain.instruments import Equity
from tradex_domain.value_objects import OrderId, Price, Quantity
from tradex_execution.fees import (
    _STT_DELIVERY,
    _STT_INTRADAY_SELL,
    FeeCalculator,
    PricingService,
)


def _make_fill(side: OrderSide, price: str = "100", qty: str = "10") -> Fill:
    instrument = Equity.of("NSE", "TEST")
    return Fill(
        order_id=OrderId(value="fee-oid"),
        instrument=instrument,
        side=side,
        quantity=Quantity(value=Decimal(qty)),
        price=Price(value=Decimal(price)),
        timestamp=datetime.now(UTC),
    )


def test_pricing_service_total_cost_with_custom_fees() -> None:
    """PricingService.total_cost includes fee calculator charges."""
    fee_calc = FeeCalculator()
    ps = PricingService(fee_calculator=fee_calc)
    fill = _make_fill(OrderSide.BUY, price="100", qty="10")
    cost = ps.total_cost(fill)
    trade_value = Decimal("100") * Decimal("10")  # 1000
    # Total cost for BUY = trade_value + fees
    assert cost.amount > trade_value
    assert cost.amount > Decimal("0")


def test_equity_delivery_stt_delivery_vs_intraday_rates() -> None:
    """STT delivery rate is higher than intraday rate on sell side."""
    turnover = Decimal("100000")  # 1L turnover
    delivery = FeeCalculator.equity_delivery(
        side=OrderSide.SELL,
        price=Decimal("1000"),
        quantity=Decimal("100"),
    )
    intraday = FeeCalculator.equity_intraday(
        side=OrderSide.SELL,
        price=Decimal("1000"),
        quantity=Decimal("100"),
    )
    # Delivery STT (0.1%) should be greater than intraday STT (0.025%)
    assert delivery.stt > intraday.stt
    # Verify exact rates: delivery = 0.1%, intraday = 0.025%
    expected_delivery_stt = (turnover * _STT_DELIVERY).quantize(Decimal("0.01"))
    expected_intraday_stt = (turnover * _STT_INTRADAY_SELL).quantize(Decimal("0.01"))
    assert delivery.stt == expected_delivery_stt
    assert intraday.stt == expected_intraday_stt


def test_equity_delivery_stt_is_charged_on_the_buy_side_too() -> None:
    """Since 1 Oct 2024 delivery STT is 0.1% on BOTH sides, not sell only.

    This is why the rate constant is ``_STT_DELIVERY`` and no longer
    ``_STT_DELIVERY_SELL``: the old name encoded the pre-2024 regime, and
    charging delivery STT on sells alone understated a delivery round trip by
    three times the total fee. Intraday is still sell-side only, so the buy
    side is what actually separates the two schedules.
    """
    turnover = Decimal("100000")  # 1L turnover
    delivery_buy = FeeCalculator.equity_delivery(
        side=OrderSide.BUY, price=Decimal("1000"), quantity=Decimal("100")
    )
    intraday_buy = FeeCalculator.equity_intraday(
        side=OrderSide.BUY, price=Decimal("1000"), quantity=Decimal("100")
    )
    assert delivery_buy.stt == (turnover * _STT_DELIVERY).quantize(Decimal("0.01"))
    assert intraday_buy.stt == Decimal("0.00")


def test_brokerage_cap_per_order_not_per_fill() -> None:
    """Per-order brokerage cap across partial fills (H2).

    Order qty 10 with 2 partial fills qty 5 each at price 10000:
    each fill's brokerage would be min(50000*0.0003=15, 20)=15.
    Without per-order cap total=30, with cap first 15 second min(15,5)=5 -> total 20.
    """
    from tradex_execution.engine import ExecutionEngine
    from tradex_execution.fill_sources import SimulatedFillSource
    from tradex_reactive.bus import ReactiveBus

    bus = ReactiveBus()
    # Use MagicMock fill source to avoid side effects but keep pipeline wiring;
    # we only exercise _apply_fee directly.
    engine = ExecutionEngine(
        bus=bus,
        fill_source=SimulatedFillSource(),
        fee_calculator=FeeCalculator(),
    )
    # Ensure engine has tracking dict (fails before fix)
    assert hasattr(engine, "_brokerage_accrued"), "engine must track _brokerage_accrued per order"

    oid = OrderId(value="cap-test-order")
    inst = Equity.of("NSE", "RELIANCE")
    price = Price(value=Decimal("10000"))
    qty5 = Quantity(value=Decimal("5"))
    fill1 = Fill(
        order_id=oid,
        instrument=inst,
        side=OrderSide.BUY,
        quantity=qty5,
        price=price,
        timestamp=datetime.now(UTC),
    )
    fill2 = Fill(
        order_id=oid,
        instrument=inst,
        side=OrderSide.BUY,
        quantity=qty5,
        price=price,
        timestamp=datetime.now(UTC),
    )

    # Need positions so on_fee has something to deduct from; create via on_fill
    engine._position_manager.on_fill(fill1)
    engine._position_manager.on_fill(fill2)

    # Capture realized PnL before fees
    pos_before = engine._cache.get_position(inst)
    assert pos_before is not None

    # Apply fees through engine (capped)
    engine._apply_fee(fill1)
    after1 = engine._brokerage_accrued.get(oid.value, Decimal("0"))
    assert after1 == Decimal("15"), f"first fill brokerage should be 15, got {after1}"

    engine._apply_fee(fill2)
    total = engine._brokerage_accrued.get(oid.value, Decimal("0"))
    assert total == Decimal("20"), f"total brokerage {total} should be capped at 20"
    assert total <= Decimal("20")

    # Total deducted fees must reflect capped brokerage (second fee reduced)
    # Also verify via FeeCalculator breakdown expectations
    bd1 = FeeCalculator.equity_intraday(
        side=OrderSide.BUY, price=Decimal("10000"), quantity=Decimal("5")
    )
    bd2 = FeeCalculator.equity_intraday(
        side=OrderSide.BUY, price=Decimal("10000"), quantity=Decimal("5")
    )
    assert bd1.broker_fee == Decimal("15.00")
    assert bd2.broker_fee == Decimal("15.00")
    # Without cap sum would be 30, with cap 20
    assert bd1.broker_fee + bd2.broker_fee == Decimal("30.00")
    assert total == Decimal("20")

    try:
        engine.shutdown()
    except Exception:
        pass
