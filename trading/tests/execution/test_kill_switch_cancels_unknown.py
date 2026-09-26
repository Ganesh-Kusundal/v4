"""Halting must reach the orders whose venue state is unknown.

``UNKNOWN`` means the submission crossed the broker boundary and the venue's
answer never arrived — the order may be live right now. The kill switch skipped
it because it was listed as terminal, so halting left the one order most likely
to be working at the broker still working. That is backwards.

``UNKNOWN`` is *unresolved*, not settled. The recovery path already treats it
that way (``_TERMINAL_AFTER_FILL`` excludes it so a late fill can advance it);
this pins the kill switch to the same reading.
"""

from __future__ import annotations

from decimal import Decimal

from tradex_domain.enums import OrderSide, OrderStatus, OrderType, TimeInForce
from tradex_domain.execution import Order
from tradex_domain.instruments import Equity
from tradex_domain.value_objects import OrderId, Price, Quantity
from tradex_execution.kill_switch import TERMINAL_STATUSES, KillSwitch
from tradex_execution.trading_cache import TradingCache

INSTRUMENT = Equity.of("NSE", "RELIANCE")


def _order(status: OrderStatus, order_id: str) -> Order:
    return Order(
        order_id=OrderId(order_id),
        instrument=INSTRUMENT,
        side=OrderSide.BUY,
        order_type=OrderType.LIMIT,
        quantity=Quantity(value=Decimal("10")),
        price=Price(value=Decimal("100")),
        time_in_force=TimeInForce.DAY,
        status=status,
    )


def _cache_with(*orders: Order) -> TradingCache:
    cache = TradingCache()
    for order in orders:
        cache.update_order(order)
    return cache


def _cancel_calls(cache: TradingCache) -> list[str]:
    cancelled: list[str] = []
    KillSwitch(cache=cache).trip(
        "test halt", cancel=lambda oid: cancelled.append(oid.value),
    )
    return cancelled


def test_an_unknown_order_is_cancelled_on_halt() -> None:
    """It may be live at the venue, so halting must reach it."""
    cache = _cache_with(_order(OrderStatus.UNKNOWN, "o-unknown"))

    assert _cancel_calls(cache) == ["o-unknown"], (
        "an UNKNOWN order was skipped on halt; it is the order most likely to "
        "be live at the broker"
    )


def test_a_settled_order_is_not_cancelled() -> None:
    """FILLED and REJECTED are settled; cancelling them is pointless noise."""
    cache = _cache_with(
        _order(OrderStatus.FILLED, "o-filled"),
        _order(OrderStatus.REJECTED, "o-rejected"),
    )

    assert _cancel_calls(cache) == []


def test_open_orders_are_still_cancelled() -> None:
    """The existing behaviour must not regress."""
    cache = _cache_with(
        _order(OrderStatus.ACK, "o-ack"),
        _order(OrderStatus.PARTIALLY_FILLED, "o-partial"),
        _order(OrderStatus.UNKNOWN, "o-unknown"),
    )

    assert sorted(_cancel_calls(cache)) == ["o-ack", "o-partial", "o-unknown"]


def test_unknown_is_not_a_terminal_status() -> None:
    """One definition of 'settled', shared by the kill switch and modify.

    UNKNOWN is unresolved: the venue still has to answer, so it must remain
    cancellable and must still be advanceable by a late fill. Two modules
    decided what terminal means and they disagreed; this pins the agreement.
    """
    from tradex_execution.engine import _TERMINAL_STATUSES as ENGINE_TERMINAL
    from tradex_execution.recovery import _TERMINAL_AFTER_FILL

    assert OrderStatus.UNKNOWN not in TERMINAL_STATUSES, (
        "UNKNOWN is unresolved, not settled — listing it as terminal makes the "
        "kill switch skip the one order that may be live"
    )
    assert OrderStatus.UNKNOWN not in ENGINE_TERMINAL
    assert OrderStatus.UNKNOWN not in _TERMINAL_AFTER_FILL

    # The two sets differ deliberately: a FILLED carrier can predate its fill
    # event, so recovery must still advance it, while the kill switch has
    # nothing to cancel on a settled order.
    assert OrderStatus.FILLED in TERMINAL_STATUSES
    assert OrderStatus.FILLED not in _TERMINAL_AFTER_FILL
    for settled in (OrderStatus.REJECTED, OrderStatus.CANCELLED):
        assert settled in TERMINAL_STATUSES
        assert settled in _TERMINAL_AFTER_FILL
