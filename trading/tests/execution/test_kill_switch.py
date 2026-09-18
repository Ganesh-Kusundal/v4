"""Tests for KillSwitch — the extracted halt gate.

Candidate 1 of the 2026-09-17 architecture review asked for the kill switch to
leave ``engine.py``. These tests cover the module directly: the gate state, the
metrics/risk propagation, and the cancel-all behaviour that used to be an
inline method on ExecutionEngine.
"""

from __future__ import annotations

from decimal import Decimal
from unittest.mock import MagicMock

from tradex_domain import Order
from tradex_domain.enums import OrderSide, OrderStatus, OrderType
from tradex_domain.value_objects import OrderId, Quantity

from tradex_trading.execution.kill_switch import KillSwitch, TERMINAL_STATUSES
from tradex_trading.execution.trading_cache import TradingCache


def _order(oid: str, status: OrderStatus) -> Order:
    """A minimal Order with the fields the kill switch actually reads."""
    return Order(
        order_id=OrderId(value=oid),
        instrument=MagicMock(),
        side=OrderSide.BUY,
        order_type=OrderType.MARKET,
        quantity=Quantity(value=Decimal("10")),
        price=None,
        time_in_force=MagicMock(),
        status=status,
    )


class TestKillSwitchState:
    """Gate state — set, clear, and the active view."""

    def test_starts_inactive(self) -> None:
        switch = KillSwitch()
        assert switch.is_set() is False
        assert switch.active is False

    def test_set_activates(self) -> None:
        switch = KillSwitch()
        switch.set()
        assert switch.is_set() is True
        assert switch.active is True

    def test_clear_deactivates(self) -> None:
        switch = KillSwitch()
        switch.set()
        switch.clear()
        assert switch.is_set() is False
        assert switch.active is False

    def test_set_is_idempotent(self) -> None:
        switch = KillSwitch()
        switch.set()
        switch.set()
        assert switch.active is True

    def test_active_setter_activates_and_clears(self) -> None:
        switch = KillSwitch()
        switch.active = True
        assert switch.is_set() is True
        switch.active = False
        assert switch.is_set() is False

    def test_active_property_reads_is_set(self) -> None:
        """``active`` delegates to ``is_set`` so a patched method is seen."""
        switch = KillSwitch()
        switch.is_set = lambda: True  # type: ignore[method-assign]
        assert switch.active is True


class TestKillSwitchTrip:
    """trip — metrics, risk gate, and cancel-all."""

    def test_trip_increments_metrics_counter(self) -> None:
        metrics = MagicMock()
        counter = MagicMock()
        metrics.counter.return_value = counter
        switch = KillSwitch(metrics=metrics)

        switch.trip("manual halt")

        metrics.counter.assert_called_once_with("kill_switch.tripped")
        counter.inc.assert_called_once()

    def test_trip_disables_risk_live_orders(self) -> None:
        risk = MagicMock()
        risk.live_orders_enabled = True
        switch = KillSwitch(risk=risk)

        switch.trip("risk breach")

        assert risk.live_orders_enabled is False

    def test_trip_activates_gate(self) -> None:
        switch = KillSwitch()
        switch.trip("halt")
        assert switch.active is True

    def test_trip_cancels_non_terminal_orders(self) -> None:
        cache = TradingCache()
        cache.set_order(_order("A", OrderStatus.NEW))
        cache.set_order(_order("B", OrderStatus.PARTIALLY_FILLED))
        switch = KillSwitch(cache=cache)
        cancelled: list[str] = []

        failures = switch.trip("halt", cancel=lambda oid: cancelled.append(oid.value))

        assert failures == []
        assert cancelled == ["A", "B"]

    def test_trip_skips_terminal_orders(self) -> None:
        """A filled or cancelled order is done; the switch leaves it alone."""
        cache = TradingCache()
        cache.set_order(_order("A", OrderStatus.FILLED))
        cache.set_order(_order("B", OrderStatus.CANCELLED))
        cache.set_order(_order("C", OrderStatus.REJECTED))
        switch = KillSwitch(cache=cache)
        cancelled: list[str] = []

        switch.trip("halt", cancel=lambda oid: cancelled.append(oid.value))

        assert cancelled == []

    def test_trip_collects_cancel_failures(self) -> None:
        """One failed cancel must not stop the rest."""

        def _cancel(oid: OrderId) -> None:
            if oid.value == "A":
                raise RuntimeError("venue down")

        cache = TradingCache()
        cache.set_order(_order("A", OrderStatus.NEW))
        cache.set_order(_order("B", OrderStatus.NEW))
        switch = KillSwitch(cache=cache)

        failures = switch.trip("halt", cancel=_cancel)

        assert failures == ["A"]

    def test_trip_without_cancel_is_halt_only(self) -> None:
        """No cancel callable → the gate still trips, nothing is cancelled."""
        cache = TradingCache()
        cache.set_order(_order("A", OrderStatus.NEW))
        switch = KillSwitch(cache=cache)

        failures = switch.trip("halt")

        assert failures == []
        assert switch.active is True

    def test_trip_without_cache_returns_no_failures(self) -> None:
        switch = KillSwitch()
        assert switch.trip("halt", cancel=MagicMock()) == []


class TestTerminalStatuses:
    """The statuses the switch treats as done."""

    def test_contains_the_four_done_states(self) -> None:
        assert OrderStatus.FILLED in TERMINAL_STATUSES
        assert OrderStatus.CANCELLED in TERMINAL_STATUSES
        assert OrderStatus.REJECTED in TERMINAL_STATUSES
        assert OrderStatus.UNKNOWN in TERMINAL_STATUSES

    def test_excludes_live_states(self) -> None:
        assert OrderStatus.NEW not in TERMINAL_STATUSES
        assert OrderStatus.PARTIALLY_FILLED not in TERMINAL_STATUSES
