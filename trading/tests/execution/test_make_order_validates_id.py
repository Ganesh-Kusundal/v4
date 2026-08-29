"""M6 — BrokerFillSource wraps empty raw-string order_id.

The principal-architect review (M6) found that ``_make_order``
(fill_sources.py:49) wraps a raw string into an ``OrderId`` without
validation: ``OrderId(value="")`` or ``OrderId(value="  ")`` produce
an empty id that propagates to the OMS cache. The fix: reject empty
strings with a clear error.

Test: ``_make_order`` (the helper that builds ``Order``) raises
``ValueError`` on an empty string. The natural caller path is
``BrokerFillSource.submit`` when the broker returns an empty id —
that's a real bug to surface clearly.
"""

from __future__ import annotations

import pytest

from tradex_trading.execution.fill_sources import _make_order
from tradex_domain.enums import OrderSide, OrderType, TimeInForce
from tradex_domain.instruments import Equity
from tradex_domain.value_objects import Price, Quantity
from decimal import Decimal
from tradex_domain.execution import OrderRequest


def _request() -> OrderRequest:
    return OrderRequest(
        instrument=Equity.of("NSE", "TEST"),
        side=OrderSide.BUY,
        order_type=OrderType.LIMIT,
        quantity=Quantity(value=Decimal("1")),
        price=Price(value=Decimal("100")),
        time_in_force=TimeInForce.DAY,
    )


def test_make_order_rejects_empty_string_id() -> None:
    """RED: ``_make_order(request, order_id="")`` raises ``ValueError``."""
    with pytest.raises(ValueError, match="empty|order_id"):
        _make_order(_request(), order_id="")


def test_make_order_rejects_whitespace_id() -> None:
    """RED: whitespace-only ids also rejected."""
    with pytest.raises(ValueError, match="empty|order_id"):
        _make_order(_request(), order_id="   ")


def test_make_order_accepts_valid_string() -> None:
    """Backward compat: a non-empty string is still wrapped."""
    order = _make_order(_request(), order_id="real-broker-id-123")
    assert order.order_id.value == "real-broker-id-123"
