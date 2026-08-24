"""Order status mapping tests — exhaustive over the domain enum.

The chart's order lines render whatever this seam produces; an unmapped
status must raise here rather than become `undefined` in the browser.
"""

from __future__ import annotations

import pytest

from tradex_domain.enums import OrderStatus
from tradex_trading.interface.chart_api import map_order_status


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
            "UNKNOWN": "rejected",
        }
        for member in OrderStatus:
            assert map_order_status(member) == expected[member.value], member

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
