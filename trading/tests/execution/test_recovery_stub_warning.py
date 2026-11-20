"""MP-6: recovery stubs are logged as strategy-orphaned."""

from __future__ import annotations

import logging
from decimal import Decimal

import pytest

from tradex_domain.enums import OrderSide, OrderType, TimeInForce
from tradex_domain.events import CashAccountInitialized, OrderFilled
from tradex_domain.execution import Fill, Order
from tradex_domain.instruments import Equity
from tradex_domain.value_objects import OrderId, Price, Quantity
from tradex_execution.recovery import InMemoryEventStore, recover_trading_cache
from tradex_execution.trading_cache import TradingCache


INSTRUMENT = Equity.of("NSE", "RELIANCE")
ORDER_ID = OrderId("o-stub")


def _fill() -> OrderFilled:
    return OrderFilled(
        fill=Fill(
            order_id=ORDER_ID,
            instrument=INSTRUMENT,
            side=OrderSide.BUY,
            quantity=Quantity(value=Decimal("4")),
            price=Price(value=Decimal("100")),
            fill_id="f-stub",
        ),
    )


def test_recovery_stub_is_accepted_and_logged(caplog) -> None:
    store = InMemoryEventStore()
    store.append(CashAccountInitialized(amount=Decimal("1000")))
    # No OrderPlaced carrier: recovery must build a stub order.
    store.append(_fill())

    cache = TradingCache()
    with caplog.at_level(logging.WARNING, logger="tradex_execution.recovery"):
        recover_trading_cache(store, cache)

    assert cache.get_order("o-stub") is not None
    assert any("cash_recovery_stub_fills_total" in r.message for r in caplog.records)
    assert any(r.levelno == logging.WARNING for r in caplog.records)
