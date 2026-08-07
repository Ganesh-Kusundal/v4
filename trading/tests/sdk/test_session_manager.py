"""Tests for sdk/session_manager.py — SessionManager registry."""

from __future__ import annotations

from unittest.mock import MagicMock

import pytest

from tradex_trading.sdk.session_manager import SessionManager

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _mock_session(name: str = "mock") -> MagicMock:
    """Create a mock TradingSession."""
    session = MagicMock()
    session.name = name
    session.stop = MagicMock()
    return session


# ---------------------------------------------------------------------------
# add() and get()
# ---------------------------------------------------------------------------

class TestAddAndGet:
    def test_add_and_get(self):
        mgr = SessionManager()
        s = _mock_session("dhan")
        mgr.add("dhan", s)
        assert mgr.get("dhan") is s

    def test_get_nonexistent_raises_key_error(self):
        mgr = SessionManager()
        with pytest.raises(KeyError, match="not registered"):
            mgr.get("missing")

    def test_first_add_becomes_active(self):
        mgr = SessionManager()
        s1 = _mock_session("first")
        s2 = _mock_session("second")
        mgr.add("first", s1)
        mgr.add("second", s2)
        assert mgr.active is s1

    def test_overwrite_session(self):
        mgr = SessionManager()
        s_old = _mock_session("old")
        s_new = _mock_session("new")
        mgr.add("slot", s_old)
        mgr.add("slot", s_new)
        assert mgr.get("slot") is s_new
        assert len(mgr) == 1


# ---------------------------------------------------------------------------
# remove()
# ---------------------------------------------------------------------------

class TestRemove:
    def test_remove_existing(self):
        mgr = SessionManager()
        s = _mock_session()
        mgr.add("x", s)
        removed = mgr.remove("x")
        assert removed is s
        assert len(mgr) == 0

    def test_remove_nonexistent_returns_none(self):
        mgr = SessionManager()
        assert mgr.remove("ghost") is None

    def test_remove_active_session_reassigns(self):
        mgr = SessionManager()
        s1 = _mock_session("a")
        s2 = _mock_session("b")
        mgr.add("a", s1)
        mgr.add("b", s2)
        # "a" is active (first added)
        mgr.remove("a")
        # active should now be "b"
        assert mgr.active is s2

    def test_remove_non_active_keeps_active(self):
        mgr = SessionManager()
        s1 = _mock_session("a")
        s2 = _mock_session("b")
        mgr.add("a", s1)
        mgr.add("b", s2)
        mgr.remove("b")
        assert mgr.active is s1


# ---------------------------------------------------------------------------
# active property and set_active()
# ---------------------------------------------------------------------------

class TestActiveSession:
    def test_active_with_no_sessions_raises(self):
        mgr = SessionManager()
        with pytest.raises(ValueError, match="No active session"):
            _ = mgr.active

    def test_set_active(self):
        mgr = SessionManager()
        s1 = _mock_session("a")
        s2 = _mock_session("b")
        mgr.add("a", s1)
        mgr.add("b", s2)
        mgr.set_active("b")
        assert mgr.active is s2

    def test_set_active_nonexistent_raises(self):
        mgr = SessionManager()
        with pytest.raises(KeyError, match="not registered"):
            mgr.set_active("nope")


# ---------------------------------------------------------------------------
# names property
# ---------------------------------------------------------------------------

class TestNamesProperty:
    def test_names_empty(self):
        mgr = SessionManager()
        assert mgr.names == []

    def test_names_returns_all(self):
        mgr = SessionManager()
        mgr.add("alpha", _mock_session())
        mgr.add("beta", _mock_session())
        assert sorted(mgr.names) == ["alpha", "beta"]


# ---------------------------------------------------------------------------
# __len__ and __iter__
# ---------------------------------------------------------------------------

class TestLenAndIter:
    def test_len_empty(self):
        mgr = SessionManager()
        assert len(mgr) == 0

    def test_len(self):
        mgr = SessionManager()
        mgr.add("a", _mock_session())
        mgr.add("b", _mock_session())
        assert len(mgr) == 2

    def test_iter(self):
        mgr = SessionManager()
        mgr.add("x", _mock_session())
        mgr.add("y", _mock_session())
        assert set(mgr) == {"x", "y"}


# ---------------------------------------------------------------------------
# close_all()
# ---------------------------------------------------------------------------

class TestCloseAll:
    def test_close_all_calls_stop_on_each(self):
        mgr = SessionManager()
        s1 = _mock_session("a")
        s2 = _mock_session("b")
        mgr.add("a", s1)
        mgr.add("b", s2)
        mgr.close_all()
        s1.stop.assert_called_once()
        s2.stop.assert_called_once()
        assert len(mgr) == 0

    def test_close_all_clears_active(self):
        mgr = SessionManager()
        mgr.add("a", _mock_session())
        mgr.close_all()
        with pytest.raises(ValueError, match="No active session"):
            _ = mgr.active

    def test_close_all_tolerates_exceptions(self):
        """stop() raising should not prevent other sessions from closing."""
        mgr = SessionManager()
        s1 = _mock_session("bad")
        s1.stop.side_effect = RuntimeError("boom")
        s2 = _mock_session("good")
        mgr.add("bad", s1)
        mgr.add("good", s2)
        mgr.close_all()  # should not raise
        s2.stop.assert_called_once()
        assert len(mgr) == 0
