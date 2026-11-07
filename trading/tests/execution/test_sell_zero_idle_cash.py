"""MP-3: sell orders self-fund fees from proceeds (zero idle cash exits)."""

from __future__ import annotations

from decimal import Decimal
from unittest.mock import MagicMock

from tradex_domain.enums import OrderSide
from tradex_domain.events import OrderFilled
from tradex_domain.execution import Fill
from tradex_domain.instruments import Equity
from tradex_domain.value_objects import OrderId, Price, Quantity
from tradex_execution.cash_ledger import CashLedger
from tradex_execution.engine import ExecutionEngine
from tradex_execution.fees import FeeCalculator
from tradex_execution.fill_sources import SimulatedFillSource
from tradex_execution.trading_cache import TradingCache

INSTRUMENT = Equity.of("NSE", "RELIANCE")


def _bus() -> MagicMock:
    bus = MagicMock()
    bus.of_type.return_value.pipe.return_value.subscribe = MagicMock()
    return bus


def test_cash_ledger_sell_zero_idle_cash_books() -> None:
    ledger = CashLedger(initial=0)
    ledger.apply_fill(
        OrderSide.SELL,
        Quantity(value=Decimal("1")),
        Price(value=Decimal("100")),
        Decimal("1"),
        "fill x",
    )
    assert ledger.cash == Decimal("99")


def test_cash_ledger_buy_zero_cash_raises() -> None:
    ledger = CashLedger(initial=0)
    import pytest

    with pytest.raises(ValueError):
        ledger.apply_fill(
            OrderSide.BUY,
            Quantity(value=Decimal("1")),
            Price(value=Decimal("100")),
            Decimal("1"),
            "fill x",
        )


def test_engine_sell_with_zero_cash_applies() -> None:
    engine = ExecutionEngine(
        bus=_bus(),
        fill_source=SimulatedFillSource(),
        cache=TradingCache(),
        cash=Decimal("0"),
        fee_calculator=FeeCalculator(),
    )
    fill = Fill(
        order_id=OrderId("o-sell"),
        instrument=INSTRUMENT,
        side=OrderSide.SELL,
        quantity=Quantity(value=Decimal("1")),
        price=Price(value=Decimal("100")),
        fill_id="f-sell",
    )
    engine._apply_fill(OrderFilled(fill=fill))  # noqa: SLF001
    assert len(engine.cache.all_positions()) == 1
