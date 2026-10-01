"""Order status mapping tests — exhaustive over the domain enum.

The chart's order lines render whatever this seam produces; an unmapped
status must raise here rather than become `undefined` in the browser.
"""

from __future__ import annotations

import pytest
from tradex_domain.enums import OrderStatus, OrderType
from tradex_interfaces.routes.chart import _map_order_type, map_order_status


class TestExhaustiveMapping:
    def test_every_enum_member_maps(self):
        """Iterate the enum itself, not a hand-copied list, so a new member
        added to OrderStatus automatically lands in this test and fails if
        the mapping was not extended."""
        expected = {
            "NEW": "pending",
            "PENDING": "pending",
            "SUBMITTED": "pending",
            "ACK": "working",
            "PARTIALLY_FILLED": "partial",
            "FILLED": "filled",
            "CANCELLED": "cancelled",
            "REJECTED": "rejected",
            # F-44: UNKNOWN is a broker row the process cannot interpret, and
            # its outcome is genuinely unknown. It must not be rendered as a
            # venue rejection, which is a different, decided answer.
            "UNKNOWN": "unknown",
        }
        for member in OrderStatus:
            assert map_order_status(member) == expected[member.value], member

    def test_unknown_is_not_rendered_as_rejected(self):
        """F-44 negative control: the two states are not the same answer.

        ``REJECTED`` means the venue refused the order. ``UNKNOWN`` means the
        venue never told us. An operator reading the chart decides whether to
        flatten a position on that line, and "we do not know" and "the venue
        said no" call for opposite actions — resubmit/verify versus move on.
        The backend already carries the distinction (``/api/charts/safety``
        ages UNKNOWN rows) and the chart was the one surface collapsing it.
        """
        assert map_order_status(OrderStatus.UNKNOWN) == "unknown"
        assert map_order_status(OrderStatus.UNKNOWN) != map_order_status(
            OrderStatus.REJECTED
        )

    def test_unmapped_value_raises_loudly(self):
        class FakeStatus:
            value = "SOME_FUTURE_STATUS"

        with pytest.raises(ValueError, match="unmapped OrderStatus"):
            map_order_status(FakeStatus())

    def test_plain_string_input_accepted(self):
        assert map_order_status("FILLED") == "filled"
        assert map_order_status("filled") == "filled"

    def test_working_set_matches_chart_isWorking(self):
        """The statuses the chart treats as 'live in the book' must be exactly
        what our working-ish states map to — otherwise lines linger or vanish."""
        chart_working = {"pending", "working", "partial"}
        ours = {map_order_status(s) for s in OrderStatus} & chart_working
        assert ours == chart_working


class _FakeOrder:
    def __init__(self, order_type):
        self.order_type = order_type


class TestOrderTypeMapping:
    """One test per domain OrderType -> chart vocabulary ('SL'/'SL-M')."""

    def test_market_maps_to_market(self):
        assert _map_order_type(_FakeOrder(OrderType.MARKET)) == "MARKET"

    def test_limit_maps_to_limit(self):
        assert _map_order_type(_FakeOrder(OrderType.LIMIT)) == "LIMIT"

    def test_stop_maps_to_sl(self):
        assert _map_order_type(_FakeOrder(OrderType.STOP)) == "SL"

    def test_stop_limit_maps_to_sl_m(self):
        assert _map_order_type(_FakeOrder(OrderType.STOP_LIMIT)) == "SL-M"

    def test_every_enum_member_maps(self):
        """Iterate the enum itself so a new OrderType member fails here if
        the mapping was not extended (mirrors the OrderStatus test above)."""
        expected = {
            "MARKET": "MARKET",
            "LIMIT": "LIMIT",
            "STOP": "SL",
            "STOP_LIMIT": "SL-M",
        }
        for member in OrderType:
            assert _map_order_type(_FakeOrder(member)) == expected[member.value], member

    def test_unmapped_value_raises_loudly(self):
        class FakeType:
            value = "SOME_FUTURE_TYPE"

        with pytest.raises(ValueError, match="unmapped OrderType"):
            _map_order_type(_FakeOrder(FakeType()))

    def test_plain_string_input_accepted(self):
        assert _map_order_type(_FakeOrder("stop")) == "SL"
