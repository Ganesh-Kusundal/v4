"""Tests for the canonical order status set definitions."""
from tradex_domain.enums import OrderStatus
from tradex_domain.order_status import (
    ACTIVE_STATUSES,
    PENDING_STATUSES,
    TERMINAL_STATUSES,
)


def test_active_statuses_is_frozenset():
    assert isinstance(ACTIVE_STATUSES, frozenset)


def test_active_statuses_members():
    assert ACTIVE_STATUSES == frozenset({
        OrderStatus.NEW,
        OrderStatus.PENDING,
        OrderStatus.SUBMITTED,
        OrderStatus.ACK,
        OrderStatus.PARTIALLY_FILLED,
    })


def test_pending_statuses_is_frozenset():
    assert isinstance(PENDING_STATUSES, frozenset)


def test_pending_statuses_members():
    assert PENDING_STATUSES == frozenset({
        OrderStatus.NEW,
        OrderStatus.PENDING,
        OrderStatus.SUBMITTED,
        OrderStatus.ACK,
    })


def test_terminal_statuses_is_frozenset():
    assert isinstance(TERMINAL_STATUSES, frozenset)


def test_terminal_statuses_members():
    assert TERMINAL_STATUSES == frozenset({
        OrderStatus.FILLED,
        OrderStatus.CANCELLED,
        OrderStatus.REJECTED,
    })


def test_pending_is_subset_of_active():
    assert PENDING_STATUSES <= ACTIVE_STATUSES


def test_active_and_terminal_are_disjoint():
    assert ACTIVE_STATUSES.isdisjoint(TERMINAL_STATUSES)


def test_unknown_in_no_set():
    assert OrderStatus.UNKNOWN not in ACTIVE_STATUSES
    assert OrderStatus.UNKNOWN not in PENDING_STATUSES
    assert OrderStatus.UNKNOWN not in TERMINAL_STATUSES
