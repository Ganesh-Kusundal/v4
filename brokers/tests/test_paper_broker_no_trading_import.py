"""Tests that PaperBroker works without tradex_trading dependency."""

from __future__ import annotations

import importlib
import sys
from datetime import datetime
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


def _session_clock() -> datetime:
    """A pinned in-session instant (Wed 2026-07-15 10:30 IST).

    ``PaperBroker``'s session gate refuses fills outside 09:15-15:30 IST, so a
    fill test that inherits the host clock passes in the morning and fails
    every evening.
    """
    return datetime(2026, 7, 15, 10, 30)


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
    import tradex_brokers.paper.adapter as adapter_mod

    # ``importlib.reload`` re-executes the module **in place**, rebinding its
    # classes. Every other module that did ``from ...adapter import X`` still
    # holds the pre-reload object, so a class the reloaded module raises is
    # invisible to their ``pytest.raises(X)``. That was real: running this file
    # before ``test_full_stack.py`` made ``test_history`` fail, because
    # ``PaperHistoryNotImplemented`` had been rebound out from under it. Snapshot
    # the namespace and put it back so the reload is invisible outside this test.
    before = dict(vars(adapter_mod))
    try:
        with patch.dict(sys.modules, blocked):
            importlib.reload(adapter_mod)
            broker = adapter_mod.PaperBroker()
            assert broker is not None
    finally:
        vars(adapter_mod).clear()
        vars(adapter_mod).update(before)
        for k, v in saved.items():
            sys.modules[k] = v


def test_paper_broker_basic_operations() -> None:
    """Test basic PaperBroker operations: submit order, get positions."""
    from tradex_brokers.paper.adapter import PaperBroker

    broker = PaperBroker(
        starting_cash=Decimal("50000"), auto_fill=True, clock=_session_clock
    )

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
