"""Reactive cash snapshot contract."""

from decimal import Decimal

from tradex_domain.enums import OrderSide, OrderType
from tradex_domain.execution import OrderRequest
from tradex_domain.instruments import Equity
from tradex_domain.value_objects import Price, Quantity
from tradex_execution.engine import ExecutionEngine
from tradex_execution.fill_sources import SimulatedFillSource
from tradex_reactive.bus import ReactiveBus

INSTRUMENT = Equity.of("NSE", "RELIANCE")


def _request(side: OrderSide) -> OrderRequest:
    return OrderRequest(
        instrument=INSTRUMENT,
        side=side,
        order_type=OrderType.MARKET,
        quantity=Quantity(value=Decimal("2")),
        price=Price(value=Decimal("100")),
    )


def test_cash_snapshot_debits_buys_and_credits_sells() -> None:
    engine = ExecutionEngine(
        ReactiveBus(),
        SimulatedFillSource(),
        cash=Decimal("1000"),
    )
    try:
        engine.submit(_request(OrderSide.BUY))
        assert engine.cash_snapshot().cash == Decimal("800")
        engine.submit(_request(OrderSide.SELL))
        assert engine.cash_snapshot().cash == Decimal("1000")
    finally:
        engine.shutdown()
