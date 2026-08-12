"""CashLedger — backtest cash account (unified pipeline, Task 1)."""

from __future__ import annotations

from decimal import Decimal

from tradex_domain import Fill, OrderSide
from tradex_domain.value_objects import Money, OrderId, Price, Quantity

from tradex_trading.execution.cash_ledger import CashLedger


def _fill(side: OrderSide, qty: int, price: float) -> Fill:
    return Fill(
        order_id=OrderId("1"),
        instrument=None,  # type: ignore[arg-type]
        side=side,
        quantity=Quantity(Decimal(qty)),
        price=Price(Decimal(str(price))),
        timestamp=None,
    )


def test_buy_debits_cash_and_sell_credits() -> None:
    led = CashLedger(Decimal("100000"))
    led.on_fill(OrderSide.BUY, Quantity(Decimal(1)), Price(Decimal("10")))
    assert led.cash == Decimal("99990")
    led.on_fee(Decimal("1"))
    assert led.cash == Decimal("99989")
    led.on_fill(OrderSide.SELL, Quantity(Decimal(1)), Price(Decimal("12")))
    assert led.cash == Decimal("100001")


def test_fee_as_money_and_total_fees() -> None:
    led = CashLedger(Decimal("1000"))
    led.on_fee(Money(amount=Decimal("2.50")))
    assert led.total_fees == Decimal("2.50")
    assert led.cash == Decimal("997.50")


def test_equity_adds_mark() -> None:
    led = CashLedger(Decimal("500"))
    assert led.equity(Decimal("250")) == Decimal("750")


def test_credit_and_restate() -> None:
    led = CashLedger(Decimal("100"))
    led.credit(Decimal("5"))
    assert led.cash == Decimal("105")
    led.restate(Decimal("-1"))
    assert led.cash == Decimal("104")
