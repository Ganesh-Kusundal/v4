"""Formal order state machine — no implicit transitions allowed."""

from __future__ import annotations

from enum import Enum


class OrderState(Enum):
    """All possible order states."""

    NEW = "NEW"
    ACK = "ACK"
    PARTIALLY_FILLED = "PARTIALLY_FILLED"
    FILLED = "FILLED"
    CANCELLED = "CANCELLED"
    REJECTED = "REJECTED"
    EXPIRED = "EXPIRED"


class InvalidStateTransition(Exception):
    """Raised when an invalid state transition is attempted."""

    pass


# (current_state, event_type) -> new_state
VALID_TRANSITIONS: dict[tuple[OrderState, str], OrderState] = {
    (OrderState.NEW, "ack"): OrderState.ACK,
    (OrderState.NEW, "fill"): OrderState.FILLED,  # Immediate fill (market)
    (OrderState.NEW, "reject"): OrderState.REJECTED,
    (OrderState.NEW, "cancel"): OrderState.CANCELLED,
    (OrderState.ACK, "fill"): OrderState.PARTIALLY_FILLED,
    (OrderState.ACK, "full_fill"): OrderState.FILLED,
    (OrderState.ACK, "cancel"): OrderState.CANCELLED,
    (OrderState.ACK, "reject"): OrderState.REJECTED,
    (OrderState.PARTIALLY_FILLED, "fill"): OrderState.PARTIALLY_FILLED,
    (OrderState.PARTIALLY_FILLED, "full_fill"): OrderState.FILLED,
    (OrderState.PARTIALLY_FILLED, "cancel"): OrderState.CANCELLED,
}

TERMINAL_STATES = frozenset(
    {
        OrderState.FILLED,
        OrderState.CANCELLED,
        OrderState.REJECTED,
        OrderState.EXPIRED,
    }
)


def transition(current: OrderState, event_type: str) -> OrderState:
    """
    Pure function: compute next state. Raises on invalid transition.

    This is the ONLY way order state changes. No exceptions.
    """
    if current in TERMINAL_STATES:
        raise InvalidStateTransition(
            f"Cannot transition from terminal state {current.value}"
        )
    key = (current, event_type)
    if key not in VALID_TRANSITIONS:
        raise InvalidStateTransition(
            f"Invalid transition: {current.value} + {event_type}"
        )
    return VALID_TRANSITIONS[key]
