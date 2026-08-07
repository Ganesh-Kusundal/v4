"""Tests for expanded session states and state machine."""

from tradex_trading.runtime.session_states import SessionState, SessionStateMachine


def test_session_state_enum_has_7_values():
    """Test SessionState enum has exactly 7 values."""
    assert len(SessionState) == 7
    assert SessionState.PRE_MARKET.value == "PRE_MARKET"
    assert SessionState.MARKET_OPEN.value == "MARKET_OPEN"
    assert SessionState.POST_MARKET.value == "POST_MARKET"
    assert SessionState.PRE_MARKET_CLOSE.value == "PRE_MARKET_CLOSE"
    assert SessionState.MARKET_CLOSED.value == "MARKET_CLOSED"
    assert SessionState.HALTED.value == "HALTED"
    assert SessionState.RECONNECTING.value == "RECONNECTING"


def test_legal_transitions_work():
    """Test that legal transitions succeed."""
    sm = SessionStateMachine(SessionState.PRE_MARKET)
    assert sm.transition(SessionState.MARKET_OPEN) is True
    assert sm.state == SessionState.MARKET_OPEN

    assert sm.transition(SessionState.POST_MARKET) is True
    assert sm.state == SessionState.POST_MARKET


def test_illegal_transitions_fail():
    """Test that illegal transitions fail and state unchanged."""
    sm = SessionStateMachine(SessionState.PRE_MARKET)
    # PRE_MARKET cannot go directly to POST_MARKET
    assert sm.transition(SessionState.POST_MARKET) is False
    assert sm.state == SessionState.PRE_MARKET


def test_session_state_machine_starts_in_initial_state():
    """Test SessionStateMachine starts in the specified initial state."""
    sm = SessionStateMachine()
    assert sm.state == SessionState.PRE_MARKET

    sm2 = SessionStateMachine(SessionState.MARKET_OPEN)
    assert sm2.state == SessionState.MARKET_OPEN


def test_full_lifecycle():
    """Test full lifecycle through all major session states."""
    sm = SessionStateMachine()
    assert sm.state == SessionState.PRE_MARKET

    assert sm.transition(SessionState.MARKET_OPEN) is True
    assert sm.state == SessionState.MARKET_OPEN

    assert sm.transition(SessionState.POST_MARKET) is True
    assert sm.state == SessionState.POST_MARKET

    assert sm.transition(SessionState.PRE_MARKET_CLOSE) is True
    assert sm.state == SessionState.PRE_MARKET_CLOSE

    assert sm.transition(SessionState.MARKET_CLOSED) is True
    assert sm.state == SessionState.MARKET_CLOSED
