"""Tests for AsyncTradingSession.

Covers:
- AsyncTradingSession can be instantiated
- state property delegates to sync session
- close() delegates to sync session.stop()
- Async context manager protocol (__aenter__ / __aexit__)
"""

from __future__ import annotations

import asyncio
from unittest.mock import MagicMock

import pytest
from tradex_domain import BrokerId

from tradex_trading.sdk.async_session import AsyncTradingSession
from tradex_trading.sdk.session import SessionState

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _mock_session(state: SessionState = SessionState.READY) -> MagicMock:
    """Create a mock TradingSession."""
    session = MagicMock()
    session.state = state
    session.broker_id = BrokerId.PAPER
    session.stop = MagicMock()
    return session


# ---------------------------------------------------------------------------
# Tests
# ---------------------------------------------------------------------------


def test_async_session_instantiation() -> None:
    """AsyncTradingSession can be instantiated with a sync session."""
    session = _mock_session()
    async_session = AsyncTradingSession(session)
    assert async_session is not None
    assert async_session.session is session


def test_state_property_delegates() -> None:
    """state property should delegate to the underlying sync session."""
    session = _mock_session(state=SessionState.READY)
    async_session = AsyncTradingSession(session)
    assert async_session.state == SessionState.READY

    # Change state on underlying
    session.state = SessionState.STOPPED
    assert async_session.state == SessionState.STOPPED


def test_broker_id_property_delegates() -> None:
    """broker_id property should delegate to the underlying sync session."""
    session = _mock_session()
    async_session = AsyncTradingSession(session)
    assert async_session.broker_id == BrokerId.PAPER


def test_stop_delegates_to_stop() -> None:
    """stop() should call session.stop() via executor."""
    session = _mock_session()
    async_session = AsyncTradingSession(session)

    asyncio.run(async_session.stop())
    session.stop.assert_called_once()


def test_async_context_manager() -> None:
    """Async context manager should enter and exit cleanly."""
    session = _mock_session()
    async_session = AsyncTradingSession(session)

    async def _run() -> None:
        async with async_session as entered:
            assert entered is async_session
        # After exit, stop should have been called
        session.stop.assert_called_once()

    asyncio.run(_run())


def test_async_context_manager_on_exception() -> None:
    """Async context manager should close even if body raises."""
    session = _mock_session()
    async_session = AsyncTradingSession(session)

    async def _run() -> None:
        with pytest.raises(RuntimeError):
            async with async_session:
                raise RuntimeError("boom")
        session.stop.assert_called_once()

    asyncio.run(_run())


def test_async_quote_uses_to_thread() -> None:
    """quote() should use asyncio.to_thread for non-blocking I/O."""
    session = _mock_session()
    session.market.quote.return_value = "fake_quote"
    async_session = AsyncTradingSession(session)

    async def _run() -> None:
        result = await async_session.quote("fake_instrument")
        assert result == "fake_quote"
        session.market.quote.assert_called_once_with("fake_instrument")

    asyncio.run(_run())


def test_async_submit_uses_to_thread() -> None:
    """submit() should use asyncio.to_thread for non-blocking I/O."""
    session = _mock_session()
    session.trade.submit.return_value = "fake_receipt"
    async_session = AsyncTradingSession(session)

    async def _run() -> None:
        result = await async_session.submit("fake_request")
        assert result == "fake_receipt"
        session.trade.submit.assert_called_once_with("fake_request")

    asyncio.run(_run())


def test_async_kill_switch_uses_to_thread() -> None:
    """kill_switch() should use asyncio.to_thread."""
    session = _mock_session()
    session.extension.kill_switch.return_value = {"status": "enabled"}
    async_session = AsyncTradingSession(session)

    async def _run() -> None:
        result = await async_session.kill_switch(enable=True)
        assert result == {"status": "enabled"}
        session.extension.kill_switch.assert_called_once_with(True)

    asyncio.run(_run())
