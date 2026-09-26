"""C4 — defensive tz check in RiskManager.check.

The principal-architect review (C4) found that engine.py:354-360 mixes
tz-aware (default ``datetime.now(UTC)``) and tz-naive (BacktestEngine
passes naive IST) timestamps in the rate-limit window subtraction.
The docstring at engine.py:304-312 documents the hazard; the code
never validates. Mixing raises ``TypeError`` deep in
``(aware - naive).total_seconds()``, or worse, silently drifts the
window boundary when the manager is reused.

The fix: at the top of ``check``, when ``now`` is provided, ensure
its tz-awareness matches the awareness of the first recorded order
in the rate-limit window. Raise ``ValueError`` with a clear message
otherwise.
"""

from __future__ import annotations

from datetime import UTC, datetime
from decimal import Decimal

import pytest
from tradex_domain.enums import OrderSide, OrderType, TimeInForce
from tradex_domain.execution import OrderRequest
from tradex_domain.instruments import Equity
from tradex_domain.value_objects import Price, Quantity

from tradex_trading.execution.engine import RiskManager


def _request() -> OrderRequest:
    return OrderRequest(
        instrument=Equity.of("NSE", "TEST"),
        side=OrderSide.BUY,
        order_type=OrderType.LIMIT,
        quantity=Quantity(value=Decimal("1")),
        price=Price(value=Decimal("100")),
        time_in_force=TimeInForce.DAY,
    )


def test_aware_then_naive_raises_clearly() -> None:
    """RED: first call aware, second call naive → ValueError, not TypeError.

    The pre-fix code at engine.py:354-360 silently calls
    ``(aware - naive).total_seconds()`` which raises ``TypeError`` deep
    in the call stack. The new behaviour: a clear ``ValueError`` at
    the top of ``check`` with the documented message.
    """
    rm = RiskManager(max_orders_per_minute=10)
    # First call: default aware.
    rm.check(_request(), now=datetime.now(UTC))
    # Second call: explicit naive.
    with pytest.raises(ValueError, match="tz"):
        rm.check(_request(), now=datetime.now())


def test_naive_then_aware_raises_clearly() -> None:
    """RED: first call naive, second call aware → ValueError."""
    rm = RiskManager(max_orders_per_minute=10)
    rm.check(_request(), now=datetime.now())
    with pytest.raises(ValueError, match="tz"):
        rm.check(_request(), now=datetime.now(UTC))


def test_consistent_aware_passes() -> None:
    """All-aware: no error."""
    rm = RiskManager(max_orders_per_minute=10)
    rm.check(_request(), now=datetime.now(UTC))
    rm.check(_request(), now=datetime.now(UTC))


def test_consistent_naive_passes() -> None:
    """All-naive: no error (matches BacktestEngine's deterministic replay)."""
    rm = RiskManager(max_orders_per_minute=10)
    rm.check(_request(), now=datetime.now())
    rm.check(_request(), now=datetime.now())


def test_default_now_is_aware() -> None:
    """Calling check() without `now` uses an aware default — same as today."""
    rm = RiskManager(max_orders_per_minute=10)
    # No exception, both calls succeed.
    rm.check(_request())
    rm.check(_request())


def test_window_does_not_carry_across_managers() -> None:
    """A fresh RiskManager has no rate-limit history; first naive call must not error.

    A blank manager with no prior orders should accept either awareness
    on its first call. The check is only meaningful once a window exists.
    """
    rm = RiskManager(max_orders_per_minute=10)
    # No prior calls — first call with naive must succeed.
    rm.check(_request(), now=datetime.now())
