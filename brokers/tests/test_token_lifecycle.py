"""Tests for DurableTokenManager — deadlock regression."""

from __future__ import annotations

import json
import tempfile
from pathlib import Path

from tradex_brokers.common.token_lifecycle import (
    DurableTokenManager,
)


class _FakePort:
    """Simple TokenLifecyclePort implementation for testing."""

    def __init__(self, token: str = "test-token") -> None:
        self._token = token
        self._expired = False
        self.refresh_count = 0

    def get_access_token(self) -> str:
        return self._token

    def refresh(self) -> str:
        self.refresh_count += 1
        self._token = f"refreshed-{self.refresh_count}"
        return self._token

    def is_expired(self) -> bool:
        return self._expired


class TestDurableTokenManagerDeadlock:
    """get_token() with state_path must not deadlock."""

    def test_get_token_with_state_path_no_deadlock(self) -> None:
        port = _FakePort()
        with tempfile.TemporaryDirectory() as tmp:
            state_path = Path(tmp) / "token.json"
            mgr = DurableTokenManager(port=port, state_path=state_path)
            # This should NOT deadlock (Lock → save_state → Lock)
            token = mgr.get_token()
            assert token == "refreshed-1"
            # Verify state was persisted
            assert state_path.exists()
            data = json.loads(state_path.read_text())
            assert data["access_token"] == "refreshed-1"

    def test_get_token_returns_cached_when_not_expired(self) -> None:
        port = _FakePort()
        mgr = DurableTokenManager(port=port)
        token1 = mgr.get_token()
        assert token1 == "refreshed-1"
        # Second call should return cached token (no new refresh)
        token2 = mgr.get_token()
        assert token2 == "refreshed-1"
        assert port.refresh_count == 1

    def test_get_token_refreshes_when_expired(self) -> None:
        port = _FakePort()
        mgr = DurableTokenManager(port=port)
        token1 = mgr.get_token()
        assert token1 == "refreshed-1"

        port._expired = True
        token2 = mgr.get_token()
        assert token2 == "refreshed-2"
        assert port.refresh_count == 2

    def test_save_and_load_state(self) -> None:
        port = _FakePort()
        with tempfile.TemporaryDirectory() as tmp:
            state_path = Path(tmp) / "token.json"
            mgr1 = DurableTokenManager(port=port, state_path=state_path)
            mgr1.get_token()
            mgr1.save_state()

            # Create a new manager that loads from the same file
            port2 = _FakePort()
            mgr2 = DurableTokenManager(port=port2, state_path=state_path)
            # The loaded token should be available without refresh
            token = mgr2.get_token()
            # It should return the loaded token (not expired)
            assert token == "refreshed-1"
