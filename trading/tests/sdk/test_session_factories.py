"""Tests for TradingSession.paper() and TradingSession.live() factories.

Covers:
- TradingSession.paper() creates a session
- Session has correct broker_id
- Session state transitions work (start → READY)
- TradingSession.live(confirm=False) raises ValueError
- Context manager protocol works
"""

from __future__ import annotations

import pytest
from tradex_domain import BrokerId

from tradex_trading.sdk.session import SessionState, TradingSession

# ---------------------------------------------------------------------------
# Tests — paper() factory
# ---------------------------------------------------------------------------


def test_paper_creates_session() -> None:
    """TradingSession.paper() should create a TradingSession instance."""
    session = TradingSession.paper(broker_id="PAPER")
    assert isinstance(session, TradingSession)


def test_paper_default_broker_id() -> None:
    """TradingSession.paper() default broker_id should be 'PAPER'."""
    session = TradingSession.paper(broker_id="PAPER")
    assert session.broker_id == BrokerId.PAPER


def test_paper_custom_broker_id() -> None:
    """TradingSession.paper(broker_id='DHAN') should set custom broker_id."""
    session = TradingSession.paper(broker_id="DHAN")
    assert session.broker_id == BrokerId.DHAN


def test_paper_returns_ready_session() -> None:
    """paper() factory returns a READY session (mirrors live()/boot())."""
    session = TradingSession.paper(broker_id="PAPER")
    assert session.state == SessionState.READY


def test_paper_start_is_idempotent() -> None:
    """Calling start() on an already-READY paper session is a no-op."""
    session = TradingSession.paper(broker_id="PAPER")
    assert session.state == SessionState.READY
    session.start()
    assert session.state == SessionState.READY


def test_paper_stop_transitions_to_stopped() -> None:
    """Calling stop() should transition to STOPPED state."""
    session = TradingSession.paper(broker_id="PAPER")
    session.start()
    session.stop()
    assert session.state == SessionState.STOPPED


def test_paper_mode_is_paper() -> None:
    """Paper session should have mode='paper'."""
    session = TradingSession.paper(broker_id="PAPER")
    assert session.mode == "paper"


# ---------------------------------------------------------------------------
# Tests — live() factory
# ---------------------------------------------------------------------------


def test_live_without_confirmation_raises() -> None:
    """TradingSession.live(confirm=False) should raise ValueError."""
    with pytest.raises(ValueError, match="Live trading requires explicit confirmation"):
        TradingSession.live(BrokerId.DHAN, confirm=False)


def test_live_default_no_confirmation_raises() -> None:
    """TradingSession.live() without confirm should raise ValueError (default confirm=False)."""
    with pytest.raises(ValueError, match="Live trading requires explicit confirmation"):
        TradingSession.live(BrokerId.DHAN)


# ---------------------------------------------------------------------------
# Tests — context manager protocol
# ---------------------------------------------------------------------------


def test_context_manager_protocol() -> None:
    """TradingSession should support context manager protocol."""
    session = TradingSession.paper(broker_id="PAPER")
    with session as s:
        assert s is session
    # After exit, session should be stopped (via close() -> stop())
    assert session.state == SessionState.STOPPED


def test_context_manager_with_start() -> None:
    """Context manager should work after start()."""
    session = TradingSession.paper(broker_id="PAPER")
    session.start()
    assert session.state == SessionState.READY

    with session as s:
        assert s is session
    # After exit, session should be stopped
    assert session.state == SessionState.STOPPED
