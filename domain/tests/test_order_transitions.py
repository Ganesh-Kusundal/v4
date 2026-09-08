"""Tests for Order state transitions."""

from __future__ import annotations

from decimal import Decimal

import pytest

from tradex_domain.enums import (
    OrderSide,
    OrderStatus,
    OrderType,
    ProductType,
    TimeInForce,
)
from tradex_domain.errors import SessionStateError
from tradex_domain.execution import Order
from tradex_domain.instruments import Equity
from tradex_domain.value_objects import OrderId, Price, Quantity

_NSE_RELIANCE = Equity.of("NSE", "RELIANCE")


def _make_order(status: OrderStatus) -> Order:
    return Order(
        order_id=OrderId("ORD-1"),
        instrument=_NSE_RELIANCE,
        side=OrderSide.BUY,
        order_type=OrderType.LIMIT,
        quantity=Quantity(Decimal("10")),
        price=Price(Decimal("100")),
        time_in_force=TimeInForce.DAY,
        status=status,
        product_type=ProductType.INTRADAY,
    )


class TestLegalTransitions:
    @pytest.mark.parametrize(
        "from_status,to_status",
        [
            (OrderStatus.NEW, OrderStatus.PENDING),
            (OrderStatus.NEW, OrderStatus.CANCELLED),
            (OrderStatus.NEW, OrderStatus.REJECTED),
            (OrderStatus.PENDING, OrderStatus.ACK),
            (OrderStatus.PENDING, OrderStatus.CANCELLED),
            (OrderStatus.PENDING, OrderStatus.REJECTED),
            (OrderStatus.ACK, OrderStatus.PARTIALLY_FILLED),
            (OrderStatus.ACK, OrderStatus.FILLED),
            (OrderStatus.ACK, OrderStatus.CANCELLED),
            (OrderStatus.ACK, OrderStatus.REJECTED),
            (OrderStatus.PARTIALLY_FILLED, OrderStatus.FILLED),
            (OrderStatus.PARTIALLY_FILLED, OrderStatus.CANCELLED),
            (OrderStatus.PARTIALLY_FILLED, OrderStatus.REJECTED),
            (OrderStatus.SUBMITTED, OrderStatus.PARTIALLY_FILLED),
            (OrderStatus.SUBMITTED, OrderStatus.FILLED),
            (OrderStatus.SUBMITTED, OrderStatus.CANCELLED),
            (OrderStatus.CANCELLED, OrderStatus.FILLED),
            (OrderStatus.CANCELLED, OrderStatus.PARTIALLY_FILLED),
        ],
    )
    def test_legal_transition(self, from_status: OrderStatus, to_status: OrderStatus):
        order = _make_order(from_status)
        new_order = order.transition_to(to_status)
        assert new_order.status == to_status
        # original is unchanged (frozen)
        assert order.status == from_status


class TestIllegalTransitions:
    @pytest.mark.parametrize(
        "from_status,to_status",
        [
            (OrderStatus.FILLED, OrderStatus.NEW),
            (OrderStatus.FILLED, OrderStatus.CANCELLED),
            (OrderStatus.CANCELLED, OrderStatus.NEW),
            (OrderStatus.REJECTED, OrderStatus.NEW),
            (OrderStatus.REJECTED, OrderStatus.ACK),
            (OrderStatus.UNKNOWN, OrderStatus.NEW),
            (OrderStatus.NEW, OrderStatus.FILLED),
            (OrderStatus.NEW, OrderStatus.ACK),
            (OrderStatus.PENDING, OrderStatus.FILLED),
        ],
    )
    def test_illegal_transition_raises(self, from_status: OrderStatus, to_status: OrderStatus):
        order = _make_order(from_status)
        with pytest.raises(SessionStateError, match="illegal order transition"):
            order.transition_to(to_status)


class TestTerminalStates:
    @pytest.mark.parametrize(
        "terminal",
        [OrderStatus.FILLED, OrderStatus.REJECTED, OrderStatus.UNKNOWN],
    )
    def test_terminal_state_has_no_outgoing(self, terminal: OrderStatus):
        order = _make_order(terminal)
        for target in OrderStatus:
            with pytest.raises(SessionStateError):
                order.transition_to(target)


class TestCancelledFillAfterCancel:
    """H3: fill-after-cancel race — venue fills a locally-cancelled order."""

    def test_cancelled_can_be_filled_on_race(self):
        order = _make_order(OrderStatus.CANCELLED)
        filled = order.transition_to(OrderStatus.FILLED)
        assert filled.status == OrderStatus.FILLED
        # original unchanged (frozen)
        assert order.status == OrderStatus.CANCELLED

    def test_cancelled_can_be_partially_filled_on_race(self):
        order = _make_order(OrderStatus.CANCELLED)
        pf = order.transition_to(OrderStatus.PARTIALLY_FILLED)
        assert pf.status == OrderStatus.PARTIALLY_FILLED

    def test_cancelled_still_rejects_other_transitions(self):
        order = _make_order(OrderStatus.CANCELLED)
        for target in (OrderStatus.NEW, OrderStatus.PENDING, OrderStatus.ACK, OrderStatus.CANCELLED, OrderStatus.REJECTED, OrderStatus.SUBMITTED, OrderStatus.UNKNOWN):
            with pytest.raises(SessionStateError):
                order.transition_to(target)


class TestFillQuantityValidation:
    def test_transition_rejects_negative_filled_quantity(self):
        order = _make_order(OrderStatus.ACK)
        with pytest.raises(ValueError, match="filled_quantity must be non-negative"):
            order.transition_to(
                OrderStatus.PARTIALLY_FILLED,
                filled_quantity=Quantity(Decimal("-1")),
            )

    def test_transition_rejects_filled_quantity_above_order_quantity(self):
        order = _make_order(OrderStatus.ACK)
        with pytest.raises(ValueError, match="filled_quantity cannot exceed quantity"):
            order.transition_to(
                OrderStatus.FILLED,
                filled_quantity=Quantity(Decimal("11")),
            )
