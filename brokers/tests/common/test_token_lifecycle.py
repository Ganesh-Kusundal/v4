"""Tests for v3-ported token lifecycle functionality.

Covers ``TokenMintResult``, ``TokenBroadcast``, ``TokenRefreshScheduler``,
and the generation-aware methods on ``DurableTokenManager`` (``ensure_token``,
``current``, ``persisted_refresh_token``, ``_expired``, ``_mint_and_persist``).
"""

from __future__ import annotations

import json
import tempfile
import time
from pathlib import Path

from tradex_brokers.common.token_lifecycle import (
    MintTokenManager,
    TokenBroadcast,
    TokenMintResult,
    TokenRefreshScheduler,
    _TokenState,
)

# ---------------------------------------------------------------------------
# TokenMintResult
# ---------------------------------------------------------------------------


class TestTokenMintResult:
    """TokenMintResult dataclass basics."""

    def test_token_only(self) -> None:
        r = TokenMintResult(token="abc")
        assert r.token == "abc"
        assert r.expires_at is None
        assert r.refresh_token is None

    def test_full_result(self) -> None:
        r = TokenMintResult(token="abc", expires_at=1000.0, refresh_token="ref")
        assert r.expires_at == 1000.0
        assert r.refresh_token == "ref"

    def test_frozen(self) -> None:
        r = TokenMintResult(token="abc")
        try:
            r.token = "other"  # type: ignore[misc]
            raise AssertionError("should be frozen")
        except AttributeError:
            pass


# ---------------------------------------------------------------------------
# _TokenState
# ---------------------------------------------------------------------------


class TestTokenState:
    """_TokenState internal dataclass."""

    def test_basic(self) -> None:
        s = _TokenState(generation=1, token="tok", issued_at="2024-01-01")
        assert s.generation == 1
        assert s.expires_at is None
        assert s.refresh_token is None

    def test_with_expiry(self) -> None:
        s = _TokenState(
            generation=3,
            token="tok",
            issued_at="2024-01-01",
            expires_at=9999.0,
            refresh_token="ref",
        )
        assert s.expires_at == 9999.0
        assert s.refresh_token == "ref"


# ---------------------------------------------------------------------------
# TokenBroadcast
# ---------------------------------------------------------------------------


class TestTokenBroadcast:
    """TokenBroadcast fan-out with error isolation."""

    def test_register_and_count(self) -> None:
        bc = TokenBroadcast()
        assert bc.receiver_count() == 0
        bc.register(lambda t: None)
        assert bc.receiver_count() == 1

    def test_broadcast_notifies_all(self) -> None:
        bc = TokenBroadcast()
        received: list[str] = []
        bc.register(lambda t: received.append(f"a:{t}"))
        bc.register(lambda t: received.append(f"b:{t}"))
        count = bc.broadcast("tok")
        assert count == 2
        assert received == ["a:tok", "b:tok"]

    def test_broadcast_isolates_errors(self) -> None:
        bc = TokenBroadcast()
        received: list[str] = []

        def bad(_t: str) -> None:
            raise RuntimeError("boom")

        bc.register(bad)
        bc.register(lambda t: received.append(t))
        count = bc.broadcast("tok")
        assert count == 2
        assert received == ["tok"]

    def test_register_returns_receiver(self) -> None:
        bc = TokenBroadcast()
        fn = lambda t: None  # noqa: E731
        result = bc.register(fn)
        assert result is fn


# ---------------------------------------------------------------------------
# TokenRefreshScheduler
# ---------------------------------------------------------------------------


class TestTokenRefreshScheduler:
    """TokenRefreshScheduler background refresh loop."""

    def test_refresh_now_success(self) -> None:
        class _FakeManager:
            def __init__(self) -> None:
                self.calls = 0

            def ensure_token(self, **_kw: object) -> str:
                self.calls += 1
                return "tok"

        mgr = _FakeManager()
        sched = TokenRefreshScheduler(mgr, interval_seconds=60.0)  # type: ignore[arg-type]
        assert sched.refresh_now() is True
        assert sched.refresh_count() == 1
        assert sched.error_count() == 0

    def test_refresh_now_failure(self) -> None:
        class _FakeManager:
            def ensure_token(self, **_kw: object) -> str:
                raise RuntimeError("fail")

        mgr = _FakeManager()
        sched = TokenRefreshScheduler(mgr, interval_seconds=60.0)  # type: ignore[arg-type]
        assert sched.refresh_now() is False
        assert sched.error_count() == 1
        assert sched.refresh_count() == 0

    def test_start_stop(self) -> None:
        class _FakeManager:
            def ensure_token(self, **_kw: object) -> str:
                return "tok"

        mgr = _FakeManager()
        sched = TokenRefreshScheduler(mgr, interval_seconds=0.05)  # type: ignore[arg-type]
        assert not sched.is_running()
        sched.start()
        assert sched.is_running()
        # Starting again is idempotent
        sched.start()
        sched.stop(timeout_seconds=2.0)
        assert not sched.is_running()


# ---------------------------------------------------------------------------
# DurableTokenManager — generation-aware (mint mode)
# ---------------------------------------------------------------------------


class TestMintTokenManagerMintMode:
    """MintTokenManager ensure_token / current / persistence in mint mode."""

    def test_legacy_deterministic_mint(self) -> None:
        """Without a mint callable, uses gen-N deterministic behavior."""
        mgr = MintTokenManager()
        tok = mgr.ensure_token()
        assert tok == "gen-1"
        tok2 = mgr.ensure_token()
        assert tok2 == "gen-2"

    def test_mint_callable_returning_str(self) -> None:
        counter = {"n": 0}

        def mint() -> str:
            counter["n"] += 1
            return f"minted-{counter['n']}"

        mgr = MintTokenManager(mint=mint)
        tok = mgr.ensure_token()
        assert tok == "minted-1"
        # Reuse valid token (no expiry set → trust until rejected)
        tok2 = mgr.ensure_token()
        assert tok2 == "minted-1"

    def test_mint_callable_returning_result(self) -> None:
        def mint() -> TokenMintResult:
            return TokenMintResult(token="oauth-tok", expires_at=time.time() + 3600)

        mgr = MintTokenManager(mint=mint)
        tok = mgr.ensure_token()
        assert tok == "oauth-tok"
        # Not expired → reuse
        tok2 = mgr.ensure_token()
        assert tok2 == "oauth-tok"

    def test_force_refresh(self) -> None:
        counter = {"n": 0}

        def mint() -> str:
            counter["n"] += 1
            return f"minted-{counter['n']}"

        mgr = MintTokenManager(mint=mint)
        mgr.ensure_token()
        tok2 = mgr.ensure_token(force_refresh=True)
        assert tok2 == "minted-2"

    def test_rejected_token_triggers_new_generation(self) -> None:
        counter = {"n": 0}

        def mint() -> str:
            counter["n"] += 1
            return f"minted-{counter['n']}"

        mgr = MintTokenManager(mint=mint)
        tok1 = mgr.ensure_token()
        assert tok1 == "minted-1"
        # Reject the current token → new generation
        tok2 = mgr.ensure_token(rejected_token=tok1)
        assert tok2 == "minted-2"

    def test_stale_rejected_token_reuses_current(self) -> None:
        counter = {"n": 0}

        def mint() -> str:
            counter["n"] += 1
            return f"minted-{counter['n']}"

        mgr = MintTokenManager(mint=mint)
        mgr.ensure_token()  # gen-1 = minted-1
        mgr.ensure_token(force_refresh=True)  # gen-2 = minted-2
        # Reject an old token → reuse current
        tok = mgr.ensure_token(rejected_token="minted-1")
        assert tok == "minted-2"

    def test_current_returns_empty_when_no_state(self) -> None:
        mgr = MintTokenManager()
        assert mgr.current() == ""

    def test_current_returns_token_after_ensure(self) -> None:
        mgr = MintTokenManager(mint=lambda: "hello")
        mgr.ensure_token()
        assert mgr.current() == "hello"

    def test_persisted_refresh_token_default(self) -> None:
        mgr = MintTokenManager()
        assert mgr.persisted_refresh_token() == ""

    def test_persisted_refresh_token_from_mint_result(self) -> None:
        def mint() -> TokenMintResult:
            return TokenMintResult(token="t", refresh_token="ref-123")

        mgr = MintTokenManager(mint=mint)
        mgr.ensure_token()
        assert mgr.persisted_refresh_token() == "ref-123"

    def test_expired_check(self) -> None:
        mgr = MintTokenManager()
        state = _TokenState(generation=1, token="t", issued_at="now", expires_at=0.0)
        assert mgr._expired(state) is True

        state2 = _TokenState(
            generation=1, token="t", issued_at="now", expires_at=time.time() + 9999
        )
        assert mgr._expired(state2) is False

        state3 = _TokenState(generation=1, token="t", issued_at="now", expires_at=None)
        assert mgr._expired(state3) is False

    def test_persistence_roundtrip(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "token.json"
            mgr1 = MintTokenManager(
                mint=lambda: TokenMintResult(token="persisted", expires_at=time.time() + 3600),
                state_path=path,
            )
            mgr1.ensure_token()

            # New manager loads from same file
            mgr2 = MintTokenManager(state_path=path)
            assert mgr2.current() == "persisted"

    def test_generation_marker_persisted(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "token.json"
            mgr = MintTokenManager(mint=lambda: "tok", state_path=path)
            mgr.ensure_token()  # gen-1
            gen_path = path.with_name("token.json.generation")
            assert gen_path.exists()
            assert gen_path.read_text().strip() == "1"

    def test_v2_legacy_durable_file_migration(self) -> None:
        """v2 durable files use access_token + expires_at_ms keys."""
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "token.json"
            v2_data = {
                "access_token": "v2-live-token",
                "expires_at_ms": (time.time() + 3600) * 1000,
            }
            path.write_text(json.dumps(v2_data))

            mgr = MintTokenManager(state_path=path)
            assert mgr.current() == "v2-live-token"

    def test_corrupt_json_resets_state(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "token.json"
            path.write_text("{bad json")
            mgr = MintTokenManager(state_path=path)
            assert mgr.current() == ""

    def test_trust_until_rejected_clamp(self) -> None:
        """Token already within buffer has expires_at clamped to None."""
        def mint() -> TokenMintResult:
            return TokenMintResult(token="short", expires_at=time.time() - 10)

        mgr = MintTokenManager(mint=mint, refresh_buffer_seconds=5.0)
        mgr.ensure_token()
        # The expires_at should have been clamped → token reused
        tok = mgr.ensure_token()
        assert tok == "short"


# ---------------------------------------------------------------------------
# Split classes: PortTokenManager / MintTokenManager direct construction
# ---------------------------------------------------------------------------


class _FakePort:
    """Minimal TokenLifecyclePort double."""

    def __init__(self) -> None:
        self.refresh_count = 0
        self.expired = False

    def get_access_token(self) -> str:
        return "port-token"

    def refresh(self) -> str:
        self.refresh_count += 1
        return f"port-token-{self.refresh_count}"

    def is_expired(self) -> bool:
        return self.expired

    def ensure_token(
        self, *, force_refresh: bool = False, rejected_token: str | None = None
    ) -> str:
        return self.get_access_token()

    def current(self) -> str:
        return self.get_access_token()


class TestPortTokenManagerDirect:
    def test_get_token_refreshes_when_no_cache(self) -> None:
        from tradex_brokers.common.token_lifecycle import PortTokenManager

        port = _FakePort()
        mgr = PortTokenManager(port=port)
        assert mgr.get_token() == "port-token-1"
        # Cached and not expired — no further refresh
        assert mgr.get_token() == "port-token-1"

    def test_force_refresh_always_refreshes(self) -> None:
        from tradex_brokers.common.token_lifecycle import PortTokenManager

        port = _FakePort()
        mgr = PortTokenManager(port=port)
        assert mgr.force_refresh() == "port-token-1"
        assert mgr.force_refresh() == "port-token-2"

    def test_get_token_with_rejection_stale_401(self) -> None:
        from tradex_brokers.common.token_lifecycle import PortTokenManager

        port = _FakePort()
        mgr = PortTokenManager(port=port)
        current = mgr.get_token()
        assert current == "port-token-1"
        # Stale 401 against an older token — reuse the newer cached token.
        assert mgr.get_token_with_rejection(rejected_token="older-token") == current

    def test_persistence_roundtrip(self) -> None:
        from tradex_brokers.common.token_lifecycle import PortTokenManager

        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "port_token.json"
            port = _FakePort()
            mgr = PortTokenManager(port=port, state_path=path)
            tok = mgr.get_token()
            assert path.exists()

            port2 = _FakePort()
            mgr2 = PortTokenManager(port=port2, state_path=path)
            assert mgr2.get_token() == tok  # loaded from disk, not refreshed
            assert port2.refresh_count == 0


class TestMintTokenManagerDirect:
    def test_mint_and_reuse(self) -> None:
        from tradex_brokers.common.token_lifecycle import MintTokenManager

        calls = {"n": 0}

        def mint() -> str:
            calls["n"] += 1
            return f"mint-{calls['n']}"

        mgr = MintTokenManager(mint=mint)
        assert mgr.ensure_token() == "mint-1"
        assert mgr.ensure_token() == "mint-1"  # reused
        assert mgr.ensure_token(force_refresh=True) == "mint-2"

    def test_durable_mode_persists_generations(self) -> None:
        from tradex_brokers.common.token_lifecycle import MintTokenManager

        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "mint_token.json"
            mgr = MintTokenManager(state_path=path, mint=lambda: "durable-mint")
            tok = mgr.ensure_token()
            assert tok == "durable-mint"

            # A second manager reading the same file reuses the durable token.
            mgr2 = MintTokenManager(state_path=path, mint=lambda: "other")
            assert mgr2.ensure_token() == "durable-mint"

    def test_satisfies_token_lifecycle_port(self) -> None:
        from tradex_brokers.common.token_lifecycle import (
            MintTokenManager,
            TokenLifecyclePort,
        )

        mgr = MintTokenManager(mint=lambda: "conform")
        assert isinstance(mgr, TokenLifecyclePort)
        assert mgr.get_access_token() == "conform"
        assert mgr.refresh() == "conform"  # force-refresh re-mints via callable
        assert mgr.is_expired() is False  # no expiry on plain-string mints

    def test_durable_compat_subclass_unchanged(self) -> None:
        from tradex_brokers.common.token_lifecycle import DurableTokenManager

        mgr = DurableTokenManager(mint=lambda: "compat")
        assert mgr.ensure_token() == "compat"
        assert mgr.get_token_with_rejection  # port-mode API still present
