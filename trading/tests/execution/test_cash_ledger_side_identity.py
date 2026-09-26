"""Contract: ``CashLedger.on_fill`` branches on ``OrderSide`` identity.

The ledger decides whether a fill *debits* (BUY) or *credits* (SELL) cash.
Getting that backwards is a silent accounting error rather than a crash, so
the branch must key off the enum member's identity.

``OrderSide`` is a ``StrEnum``, so a lookalike carrying the same text is
``== "BUY"`` but is not ``OrderSide.BUY``. The identity check means only the
member itself debits; anything unrecognised is read as a SELL and credited,
which keeps the credit (non-destructive) branch as the default.
"""

from __future__ import annotations

from decimal import Decimal
from enum import StrEnum

from tradex_domain.enums import OrderSide
from tradex_domain.value_objects import Price, Quantity

from tradex_trading.execution.cash_ledger import CashLedger


class _ForeignSide(StrEnum):
    """A different StrEnum reusing the same wire values."""

    BUY = "BUY"
    SELL = "SELL"


class _DuckSide:
    """A non-enum object exposing ``.value``."""

    def __init__(self, value: str) -> None:
        self.value = value


def _ledger(initial: str = "1000") -> CashLedger:
    return CashLedger(Decimal(initial))


_QTY = Quantity(value=Decimal("1"))
_PRICE = Price(value=Decimal("10"))


class TestGenuineSides:
    """The enum members' debit/credit behaviour is unchanged."""

    def test_buy_debits_cash(self) -> None:
        led = _ledger()
        led.on_fill(OrderSide.BUY, _QTY, _PRICE)
        assert led.cash == Decimal("990")

    def test_sell_credits_cash(self) -> None:
        led = _ledger()
        led.on_fill(OrderSide.SELL, _QTY, _PRICE)
        assert led.cash == Decimal("1010")

    def test_buy_then_sell_round_trips_with_the_fee(self) -> None:
        led = _ledger("100000")
        led.on_fill(OrderSide.BUY, Quantity(value=Decimal("2")), Price(value=Decimal("100")))
        assert led.cash == Decimal("99800")
        led.on_fee(Decimal("1"))
        led.on_fill(OrderSide.SELL, Quantity(value=Decimal("1")), Price(value=Decimal("120")))
        # 100000 − 200 notional − 1 fee + 120 proceeds.
        assert led.cash == Decimal("99919")

    def test_buy_still_respects_the_buying_power_floor(self) -> None:
        """Identity must not weaken the fail-closed debit guard."""
        led = _ledger("5")
        try:
            led.on_fill(OrderSide.BUY, _QTY, _PRICE)
        except ValueError as exc:
            assert "insufficient buying power" in str(exc)
        else:
            raise AssertionError("expected ValueError on an unaffordable BUY")
        assert led.cash == Decimal("5")

    def test_sell_credit_still_works_when_negative_is_disallowed(self) -> None:
        led = CashLedger(Decimal("0"), allow_negative=False)
        led.on_fill(OrderSide.SELL, _QTY, _PRICE)
        assert led.cash == Decimal("10")


class TestNonMemberSidesCredit:
    """Only the enum member itself debits."""

    def test_lookalike_buy_does_not_debit(self) -> None:
        """A non-member claiming ``.value == "BUY"`` is credited instead.

        Under the old ``side.value == "BUY"`` comparison this DEBITED 10.
        """
        led = _ledger()
        led.on_fill(_DuckSide("BUY"), _QTY, _PRICE)  # type: ignore[arg-type]
        assert led.cash == Decimal("1010")

    def test_foreign_str_enum_buy_does_not_debit(self) -> None:
        led = _ledger()
        led.on_fill(_ForeignSide.BUY, _QTY, _PRICE)  # type: ignore[arg-type]
        assert led.cash == Decimal("1010")

    def test_lookalike_sell_credits_like_the_real_member(self) -> None:
        expected = _ledger()
        expected.on_fill(OrderSide.SELL, _QTY, _PRICE)
        for side in (_DuckSide("SELL"), _ForeignSide.SELL):
            led = _ledger()
            led.on_fill(side, _QTY, _PRICE)  # type: ignore[arg-type]
            assert led.cash == expected.cash
