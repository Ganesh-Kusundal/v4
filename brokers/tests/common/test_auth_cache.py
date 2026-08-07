"""Ported from v3 ``test_auth_cache.py`` — ReadCache + DurableTokenManager.

v4 ReadCache API is compatible with v3 (``get``/``set``/``expired_count``).
v4 DurableTokenManager uses ``TokenLifecyclePort`` protocol (completely
different from v3's ``mint=`` callback).  Auth-mint tests
(``dhan_totp_mint``, ``upstox_refresh_mint``) are skipped — they depend
on broker-specific auth flows.
"""

from __future__ import annotations

import json
from pathlib import Path

from tradex_domain import AuthenticationError

from tradex_brokers.common.cache import ReadCache
from tradex_brokers.common.token_lifecycle import (
    DurableTokenManager,
)


class _FakePort:
    """Minimal TokenLifecyclePort for testing."""

    def __init__(self, token: str = "tok-1") -> None:
        self._token = token
        self._expired = False
        self.refresh_count = 0

    def get_access_token(self) -> str:
        return self._token

    def refresh(self) -> str:
        self.refresh_count += 1
        self._token = f"tok-{self.refresh_count + 1}"
        return self._token

    def is_expired(self) -> bool:
        return self._expired


# ---------------------------------------------------------------------------
# ReadCache — TTL hits, expiry, invalidation
# ---------------------------------------------------------------------------


def test_read_cache_hits_until_ttl_then_expires() -> None:
    now = [10.0]
    cache = ReadCache(clock=lambda: now[0])

    assert cache.get("quote") is None
    cache.set("quote", {"ltp": 100}, ttl_seconds=5.0)
    assert cache.get("quote") == {"ltp": 100}
    now[0] = 15.1
    assert cache.get("quote") is None
    assert cache.expired_count == 1


def test_read_cache_invalidate_specific_key() -> None:
    cache = ReadCache()
    cache.set("a", 1, ttl_seconds=60.0)
    cache.set("b", 2, ttl_seconds=60.0)
    cache.invalidate("a")
    assert cache.get("a") is None
    assert cache.get("b") == 2


def test_read_cache_invalidate_all() -> None:
    cache = ReadCache()
    cache.set("a", 1, ttl_seconds=60.0)
    cache.set("b", 2, ttl_seconds=60.0)
    cache.invalidate()
    assert cache.get("a") is None
    assert cache.get("b") is None
    assert cache.size == 0


def test_read_cache_hits_and_misses_counters() -> None:
    cache = ReadCache()
    cache.set("k", "v", ttl_seconds=60.0)
    cache.get("k")  # hit
    cache.get("missing")  # miss
    assert cache.hits == 1
    assert cache.misses == 1


# ---------------------------------------------------------------------------
# DurableTokenManager — get_token, refresh, persistence
# ---------------------------------------------------------------------------


def test_durable_manager_reuses_valid_token(tmp_path: Path) -> None:
    port = _FakePort()
    manager = DurableTokenManager(port=port, state_path=tmp_path / "token.json")
    t1 = manager.get_token()
    t2 = manager.get_token()
    assert t1 == t2
    assert port.refresh_count == 1  # only one refresh (cached)


def test_durable_manager_refreshes_when_expired(tmp_path: Path) -> None:
    port = _FakePort()
    manager = DurableTokenManager(port=port, state_path=tmp_path / "token.json")
    t1 = manager.get_token()
    port._expired = True
    t2 = manager.get_token()
    assert t1 != t2
    assert port.refresh_count == 2


def test_durable_manager_force_refresh(tmp_path: Path) -> None:
    port = _FakePort()
    manager = DurableTokenManager(port=port, state_path=tmp_path / "token.json")
    t1 = manager.get_token()
    t2 = manager.force_refresh()
    assert t1 != t2
    assert port.refresh_count == 2


def test_durable_manager_persists_state(tmp_path: Path) -> None:
    port = _FakePort()
    state_path = tmp_path / "token.json"
    manager = DurableTokenManager(port=port, state_path=state_path)
    token = manager.get_token()
    manager.save_state()
    assert state_path.exists()
    data = json.loads(state_path.read_text())
    assert data["access_token"] == token


def test_durable_manager_loads_persisted_state(tmp_path: Path) -> None:
    state_path = tmp_path / "token.json"
    state_path.write_text(json.dumps({"access_token": "persisted-token"}))

    port = _FakePort()
    manager = DurableTokenManager(port=port, state_path=state_path)
    # Should load the persisted token without refreshing
    assert manager._cached_token == "persisted-token"


def test_durable_manager_refresh_failure_raises_auth_error(tmp_path: Path) -> None:
    class _FailingPort:
        def get_access_token(self) -> str:
            return ""

        def refresh(self) -> str:
            raise RuntimeError("provider down")

        def is_expired(self) -> bool:
            return True

    manager = DurableTokenManager(port=_FailingPort(), state_path=tmp_path / "token.json")
    import pytest

    with pytest.raises(AuthenticationError, match="Token refresh failed"):
        manager.get_token()


def test_durable_manager_without_state_path() -> None:
    port = _FakePort()
    manager = DurableTokenManager(port=port)
    token = manager.get_token()
    assert token  # non-empty token returned
    # save_state is a no-op without a path
    manager.save_state()
