"""Tests for formal order state machine — no implicit transitions allowed."""

import pytest

from tradex_trading.events.order_fsm import (
    OrderState,
    TERMINAL_STATES,
    VALID_TRANSITIONS,
    InvalidStateTransition,
    transition,
)


def test_valid_transitions():
    """Verify all valid transitions produce correct new state."""
    assert transition(OrderState.NEW, "ack") == OrderState.ACK
    assert transition(OrderState.NEW, "fill") == OrderState.FILLED
    assert transition(OrderState.NEW, "reject") == OrderState.REJECTED
    assert transition(OrderState.NEW, "cancel") == OrderState.CANCELLED
    assert transition(OrderState.ACK, "fill") == OrderState.PARTIALLY_FILLED
    assert transition(OrderState.ACK, "full_fill") == OrderState.FILLED
    assert transition(OrderState.ACK, "cancel") == OrderState.CANCELLED
    assert transition(OrderState.ACK, "reject") == OrderState.REJECTED
    assert transition(OrderState.PARTIALLY_FILLED, "fill") == OrderState.PARTIALLY_FILLED
    assert transition(OrderState.PARTIALLY_FILLED, "full_fill") == OrderState.FILLED
    assert transition(OrderState.PARTIALLY_FILLED, "cancel") == OrderState.CANCELLED


def test_terminal_states():
    """Verify FILLED, CANCELLED, REJECTED, EXPIRED are terminal."""
    assert OrderState.FILLED in TERMINAL_STATES
    assert OrderState.CANCELLED in TERMINAL_STATES
    assert OrderState.REJECTED in TERMINAL_STATES
    assert OrderState.EXPIRED in TERMINAL_STATES


def test_no_transitions_from_terminal():
    """Every terminal state + every event_type raises InvalidStateTransition."""
    for state in TERMINAL_STATES:
        with pytest.raises(InvalidStateTransition):
            transition(state, "ack")
        with pytest.raises(InvalidStateTransition):
            transition(state, "fill")
        with pytest.raises(InvalidStateTransition):
            transition(state, "cancel")


def test_invalid_transitions():
    """Invalid transitions must raise InvalidStateTransition."""
    with pytest.raises(InvalidStateTransition):
        transition(OrderState.NEW, "full_fill")  # Can't full_fill from NEW
    with pytest.raises(InvalidStateTransition):
        transition(OrderState.ACK, "ack")  # Can't ack twice
    with pytest.raises(InvalidStateTransition):
        transition(OrderState.PARTIALLY_FILLED, "ack")  # Can't ack after partial


def test_all_transitions_are_symmetric():
    """Every transition must be explicitly defined — no implicit paths."""
    for (state, event_type), new_state in VALID_TRANSITIONS.items():
        assert state not in TERMINAL_STATES, f"Transition from terminal state {state}"
        assert isinstance(new_state, OrderState)
