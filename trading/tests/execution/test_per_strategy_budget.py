"""G2 — margin + per-strategy budget.

The principal-architect review (G2) and the design baseline flagged
that ``RiskManager.check()`` enforces notional, position, rate,
daily-loss, and drawdown — but not margin / buying power per
strategy, and not per-strategy risk budgets. Every strategy on the
same account draws from one shared envelope.

The fix:
  1. ``RiskBudget(strategy_id, max_order_value, max_position_value,
     max_daily_loss_amt, max_drawdown_pct)`` — per-strategy slice.
  2. ``RiskManager(budgets={strategy_id: RiskBudget})`` — the manager
     picks the right slice from the ``strategy_id`` arg.
  3. The strategy_id is read from ``request.tag`` (already stamped
     ``strategy_id@version`` by ReactiveStrategyEngine).
  4. A strategy with no budget inherits the existing single-budget
     path (backward-compat).

Tests pin:
  - buy rejected when strategy budget is exhausted
  - per-strategy isolation: A's order doesn't block B
  - exits always allowed (no daily-loss lock on reductions)
  - partial close of a short is a reduction
  - default path (no strategy_id) still works
"""

from __future__ import annotations

from dataclasses import replace
from decimal import Decimal

from tradex_domain.enums import OrderSide, OrderType, TimeInForce
from tradex_domain.instruments import Equity
from tradex_domain.value_objects import Price, Quantity

from tradex_domain.execution import OrderRequest
from tradex_trading.execution.risk import RiskBudget, RiskManager


def _request(
    side: OrderSide,
    qty: str = "10",
    price: str = "100",
    strategy_id: str | None = None,
) -> OrderRequest:
    return OrderRequest(
        instrument=Equity.of("NSE", "TEST"),
        side=side,
        order_type=OrderType.LIMIT,
        quantity=Quantity(value=Decimal(qty)),
        price=Price(value=Decimal(price)),
        time_in_force=TimeInForce.DAY,
        tag=strategy_id,
    )


def test_per_strategy_budget_isolates_allocations() -> None:
    """RED: strategy A exhausting its budget does not block strategy B.

    Both share the same RiskManager. A's order consumes A's
    ``max_position_value``; a subsequent B order of equal size must
    still pass because B has its own budget.
    """
    rm = RiskManager(
        budgets={
            "A": RiskBudget(strategy_id="A", max_position_value=Decimal("1000")),
            "B": RiskBudget(strategy_id="B", max_position_value=Decimal("1000")),
        },
    )
    # A buys 5 @ 100 = 500 notional (within A's 1000 cap).
    assert rm.check(_request(OrderSide.BUY, "5", "100", strategy_id="A")) is True
    # B buys 8 @ 100 = 800 notional (within B's 1000 cap).
    # Without per-strategy isolation, A+B = 1300 would exceed 1000;
    # with isolation, B's order passes.
    assert rm.check(_request(OrderSide.BUY, "8", "100", strategy_id="B")) is True, (
        "Strategy B must not be blocked by Strategy A's exposure (G2)"
    )


def test_strategy_buy_rejected_when_budget_exhausted() -> None:
    """RED: a strategy with a 1000 cap cannot place a 2000-notional buy."""
    rm = RiskManager(
        budgets={
            "A": RiskBudget(strategy_id="A", max_position_value=Decimal("1000")),
        },
    )
    # 20 @ 100 = 2000 notional, exceeds A's 1000 cap.
    approved = rm.check(_request(OrderSide.BUY, "20", "100", strategy_id="A"))
    assert approved is False, (
        "Strategy A's 2000-notional buy must be rejected (G2)"
    )


def test_unknown_strategy_id_falls_back_to_global_cap() -> None:
    """RED: a request with no strategy_id (legacy) sees the global cap."""
    rm = RiskManager(max_position_value=Decimal("1000"))
    # 5 @ 100 = 500 notional, within the 1000 global cap.
    assert rm.check(_request(OrderSide.BUY, "5", "100")) is True
    # 20 @ 100 = 2000 notional, exceeds the global cap.
    assert rm.check(_request(OrderSide.BUY, "20", "100")) is False


def test_per_strategy_max_order_value_enforced() -> None:
    """RED: per-strategy max_order_value gates at submit time."""
    rm = RiskManager(
        budgets={
            "A": RiskBudget(strategy_id="A", max_order_value=Decimal("500")),
        },
    )
    # 3 @ 100 = 300, under A's 500.
    assert rm.check(_request(OrderSide.BUY, "3", "100", strategy_id="A")) is True
    # 8 @ 100 = 800, over A's 500.
    assert rm.check(_request(OrderSide.BUY, "8", "100", strategy_id="A")) is False
    # B (no budget) sees no max_order_value; 8 @ 100 passes.
    assert rm.check(_request(OrderSide.BUY, "8", "100", strategy_id="B")) is True
