"""AC-9 / lane 2c — per-strategy budgets, per-side gross exposure, mark freshness.

Each test names a negative control: the assertion is only meaningful because a
neighbouring case shows the opposite outcome for the same code path.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from decimal import Decimal

from trading.tests.risk_support import FrozenClock

from tradex_domain.enums import OrderSide, OrderType, TimeInForce
from tradex_domain.execution import OrderRequest, Position
from tradex_domain.instruments import Equity
from tradex_domain.risk_budget import resolve_strategy_id
from tradex_domain.value_objects import Money, Price, Quantity
from tradex_execution.risk import RiskBudget, RiskManager

_INST = Equity.of("NSE", "TEST")
_CLOCK = FrozenClock()


def _request(
    side: OrderSide = OrderSide.BUY,
    qty: str = "10",
    price: str = "100",
    tag: str | None = None,
) -> OrderRequest:
    return OrderRequest(
        instrument=_INST,
        side=side,
        order_type=OrderType.LIMIT,
        quantity=Quantity(value=Decimal(qty)),
        price=Price(value=Decimal(price)),
        time_in_force=TimeInForce.DAY,
        tag=tag,
    )


def _position(
    qty: str = "100",
    unrealized: str = "0",
    *,
    mark: str | None = "100",
    marked_at: datetime | None = None,
) -> Position:
    inst = Equity.of("NSE", "TEST")
    return Position(
        instrument=inst,
        quantity=Quantity(value=Decimal(qty)),
        avg_price=Price(value=Decimal("100")),
        realized_pnl=Money(amount=Decimal("0")),
        unrealized_pnl=Money(amount=Decimal(unrealized)),
        mark_price=Price(value=Decimal(mark)) if mark is not None else None,
        marked_at=marked_at,
        mark_source="TEST" if mark is not None else None,
    )


# --- _active_budget resolution (the "never matched" bug) -------------------

def test_versioned_tag_resolves_the_strategy_budget() -> None:
    """A budget keyed by the bare strategy id is found through a
    ``strategy_id@version`` tag. The old code compared the whole tag, so this
    could never match and every order fell through to the global cap.
    """
    rm = RiskManager(
        max_position_value=Decimal("999999"),
        budgets={
            "sma_cross": RiskBudget(
                strategy_id="sma_cross", max_position_value=Decimal("500")
            )
        },
    )
    # 20 @ 100 = 2000, far over the strategy budget of 500 but UNDER the global
    # cap of 999999 — so this deny can only have come from the resolved budget.
    assert rm.check(_request(qty="20", tag="sma_cross@1.0.0")) is False


def test_versioned_tag_still_admits_within_budget() -> None:
    """Negative control for the test above: the same versioned tag is admitted
    when the order is inside the resolved strategy budget. Without this, a
    resolver that matched *everything* would also pass the deny case."""
    rm = RiskManager(
        budgets={
            "sma_cross": RiskBudget(
                strategy_id="sma_cross", max_position_value=Decimal("500")
            )
        },
    )
    assert rm.check(_request(qty="2", tag="sma_cross@1.0.0")) is True


def test_explicit_strategy_id_field_wins_over_tag() -> None:
    """When a real ``strategy_id`` field is present (the AC-9 data model), it
    is the authority. Proven with a stand-in object so the test is correct both
    before and after that field lands on OrderRequest."""

    class _WithId:
        strategy_id = "alpha"
        tag = "beta@2.0.0"

    assert resolve_strategy_id(_WithId()) == "alpha"


def test_tag_is_the_fallback_when_no_strategy_id_field() -> None:
    """Negative control: with no ``strategy_id`` field, the resolver falls back
    to the tag, so the strategy budget still resolves pre-merge."""

    class _NoId:
        tag = "beta@2.0.0"

    assert resolve_strategy_id(_NoId()) == "beta"


def test_unattributable_request_falls_back_to_the_global_cap() -> None:
    """A request with neither a strategy id nor a tag inherits the global caps
    rather than borrowing another strategy's budget."""

    class _Nothing:
        tag = None

    rm = RiskManager(
        max_position_value=Decimal("1000"),
        budgets={
            "sma_cross": RiskBudget(
                strategy_id="sma_cross", max_position_value=Decimal("500")
            )
        },
    )
    assert rm._active_budget(_Nothing())[1] == Decimal("1000")


# --- per-side gross exposure (was a net test) -----------------------------

def test_sell_past_a_long_is_an_increase_not_a_reduction() -> None:
    """The spec's negative control: a SELL of 150 against a LONG 100 is
    classified as INCREASING exposure, because it opens a 50-lot short that
    the old net test scored as a reduction (100^2 > 50^2)."""
    rm = RiskManager(positions_provider=lambda: [_position(qty="100")])
    assert rm._increases_exposure(_request(side=OrderSide.SELL, qty="150")) is True


def test_sell_inside_a_long_is_a_reduction() -> None:
    """Negative control: a SELL of 40 against a LONG 100 nets down to 60 and
    grows neither the long nor the short side, so it is a reduction."""
    rm = RiskManager(positions_provider=lambda: [_position(qty="100")])
    assert (
        rm._increases_exposure(_request(side=OrderSide.SELL, qty="40")) is False
    )


def test_buy_past_a_short_is_an_increase() -> None:
    """The mirror of the spec's control: a BUY of 150 against a SHORT 100
    opens a 50-lot long and is an increase."""
    rm = RiskManager(positions_provider=lambda: [_position(qty="-100")])
    assert rm._increases_exposure(_request(side=OrderSide.BUY, qty="150")) is True


def test_flattening_a_long_is_a_reduction() -> None:
    """Negative control: selling the whole long is a pure reduction."""
    rm = RiskManager(positions_provider=lambda: [_position(qty="100")])
    assert (
        rm._increases_exposure(_request(side=OrderSide.SELL, qty="100")) is False
    )


# --- mark freshness: fail closed, never a reset zero ----------------------

def test_stale_mark_ten_minutes_old_fails_the_guard_closed() -> None:
    """The spec's negative control: a position whose last mark is ten minutes
    old and fully under water. ``unrealized_pnl`` was reset to 0 by the fill
    that produced it, so reading it would report a drawdown of exactly zero and
    wave the order through. The guard must refuse instead."""
    ten_min_ago = datetime.now(UTC) - timedelta(minutes=10)
    rm = RiskManager(
        max_daily_loss_amt=Decimal("1000"),
        max_mark_age_seconds=5.0,
        positions_provider=lambda: [
            _position(qty="100", unrealized="0", marked_at=ten_min_ago)
        ],
        clock=_CLOCK,
    )
    assert rm.check(_request(side=OrderSide.BUY, qty="10")) is False
    assert rm._last_deny_reason == "marks_not_fresh"


def test_a_fresh_mark_lets_the_daily_loss_guard_read_the_real_loss() -> None:
    """Negative control for the refusal above: with a current mark the same
    guard evaluates normally, so a stale mark is what denied it and not the
    gate being wired shut."""
    stamp = _CLOCK.now()
    rm = RiskManager(
        max_daily_loss_amt=Decimal("1000"),
        max_mark_age_seconds=5.0,
        positions_provider=lambda: [
            _position(qty="100", unrealized="0", marked_at=stamp)
        ],
        clock=_CLOCK,
    )
    assert rm.check(_request(side=OrderSide.BUY, qty="10")) is True


def test_an_unmarked_open_position_fails_closed() -> None:
    """A fill clears ``mark_price``; an open position with no mark at all is
    the same defect with no timestamp to age, and is refused."""
    rm = RiskManager(
        max_daily_loss_amt=Decimal("1000"),
        max_mark_age_seconds=5.0,
        positions_provider=lambda: [_position(qty="100", mark=None)],
        clock=_CLOCK,
    )
    assert rm.check(_request(side=OrderSide.BUY, qty="10")) is False
    assert rm._last_deny_reason == "marks_not_fresh"


def test_a_reduction_is_still_allowed_while_the_mark_is_stale() -> None:
    """Negative control: risk is never locked on a position. A stale mark must
    not trap the operator in an open, losing position."""
    stale = datetime.now(UTC) - timedelta(minutes=10)
    rm = RiskManager(
        max_daily_loss_amt=Decimal("1000"),
        max_mark_age_seconds=5.0,
        positions_provider=lambda: [
            _position(qty="100", mark=None, marked_at=stale)
        ],
        clock=_CLOCK,
    )
    assert rm.check(_request(side=OrderSide.SELL, qty="100")) is True


def test_a_flat_position_is_never_stale() -> None:
    """Negative control: a closed position carries no mark and is not stale —
    it carries no exposure, so there is nothing for the guard to misread."""
    rm = RiskManager(
        max_daily_loss_amt=Decimal("1000"),
        positions_provider=lambda: [_position(qty="0", mark=None)],
        clock=_CLOCK,
    )
    assert rm._stale_marks(None) is False
