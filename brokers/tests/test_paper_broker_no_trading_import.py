"""Tests that PaperBroker works without tradex_trading dependency."""

from __future__ import annotations

import importlib
import sys
from decimal import Decimal
from unittest.mock import patch

from tradex_domain import (
    Equity,
    OrderRequest,
    OrderSide,
    OrderType,
    Price,
    Quantity,
)


def _eq() -> Equity:
    return Equity.of("NSE", "RELIANCE")


def test_paper_broker_instantiation_without_trading() -> None:
    """PaperBroker can be created even if tradex_trading is not importable."""
    # Ensure no cached import of tradex_trading interferes
    saved = {k: v for k, v in sys.modules.items() if k.startswith("tradex_trading")}
    for k in list(saved):
        del sys.modules[k]

    blocked = {
        "tradex_trading": None,
        "tradex_trading.execution": None,
        "tradex_trading.execution.trading_cache": None,
    }
    try:
        with patch.dict(sys.modules, blocked):
            import tradex_brokers.paper.adapter as adapter_mod

            importlib.reload(adapter_mod)
            broker = adapter_mod.PaperBroker()
            assert broker is not None
    finally:
        for k, v in saved.items():
            sys.modules[k] = v


def test_paper_broker_basic_operations() -> None:
    """Test basic PaperBroker operations: submit order, get positions."""
    from tradex_brokers.paper.adapter import PaperBroker

    broker = PaperBroker(starting_cash=Decimal("50000"), auto_fill=True)

    # Seed a quote for the instrument
    eq = _eq()
    broker.set_quote(eq, ltp=Price(value=Decimal("100")))

    # Submit an order
    request = OrderRequest(
        instrument=eq,
        side=OrderSide.BUY,
        order_type=OrderType.MARKET,
        quantity=Quantity(value=Decimal("10")),
        price=Price(value=Decimal("100")),
    )
    order_id = broker.submit_order(request)
    assert order_id.value.startswith("PAPER-")

    # Check positions
    positions = broker.get_positions()
    assert len(positions) == 1
    assert positions[0].quantity.value == Decimal("10")

    # Check account
    account = broker.get_account()
    assert account.balance.amount == Decimal("49000")  # 50000 - 10*100
