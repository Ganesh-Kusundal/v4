"""Expanded session states and state machine for trading session lifecycle."""

from __future__ import annotations

from enum import Enum


class SessionState(Enum):
    PRE_MARKET = "PRE_MARKET"
    MARKET_OPEN = "MARKET_OPEN"
    POST_MARKET = "POST_MARKET"
    PRE_MARKET_CLOSE = "PRE_MARKET_CLOSE"
    MARKET_CLOSED = "MARKET_CLOSED"
    HALTED = "HALTED"
    RECONNECTING = "RECONNECTING"


# Legal transitions
_LEGAL_TRANSITIONS = {
    SessionState.PRE_MARKET: {SessionState.MARKET_OPEN, SessionState.HALTED},
    SessionState.MARKET_OPEN: {
        SessionState.POST_MARKET, SessionState.HALTED, SessionState.RECONNECTING,
    },
    SessionState.POST_MARKET: {SessionState.PRE_MARKET_CLOSE, SessionState.HALTED},
    SessionState.PRE_MARKET_CLOSE: {SessionState.MARKET_CLOSED},
    SessionState.MARKET_CLOSED: {SessionState.PRE_MARKET},
    SessionState.HALTED: {SessionState.MARKET_OPEN, SessionState.MARKET_CLOSED},
    SessionState.RECONNECTING: {SessionState.MARKET_OPEN, SessionState.HALTED},
}


class SessionStateMachine:
    def __init__(self, initial: SessionState = SessionState.PRE_MARKET):
        self._state = initial

    @property
    def state(self) -> SessionState:
        return self._state

    def transition(self, new_state: SessionState) -> bool:
        """Transition to new state if legal. Returns True if successful."""
        allowed = _LEGAL_TRANSITIONS.get(self._state, set())
        if new_state in allowed:
            self._state = new_state
            return True
        return False
