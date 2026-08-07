"""Live token-path integration tests.

Proves the env-built live brokers (``build_dhan_from_env`` /
``build_upstox_from_env`` with an injected fetch) never mint on connect and
only mint when the persisted token is missing, expired (fake clock advanced
past expiry), or 401-rejected.  A valid persisted token is reused on the
wire for every authenticated call.

The token manager is exercised through the real live path: the broker's
``get_account()`` drives the client's ``token_provider`` (= ``ensure_token``)
on every request, and a 401 response drives ``on_auth_failure``
(= ``ensure_token(rejected_token=...)``) with an automatic retry.
"""

from __future__ import annotations

import base64
import json
from decimal import Decimal
from pathlib import Path

from tradex_brokers.common.totp_cooldown import TotpCooldownGuard

from tradex_trading.runtime.live import build_dhan_from_env, build_upstox_from_env

# Valid RFC-6238 test secret — the injected mint ignores the generated code.
_TOTP_SECRET = "JBSWY3DPEHPK3PXP"


def _fake_jwt(exp: float) -> str:
    """Minimal JWT carrying an ``exp`` claim (production Dhan mints are JWTs)."""
    header = base64.urlsafe_b64encode(b'{"alg":"none"}').rstrip(b"=").decode("ascii")
    payload = base64.urlsafe_b64encode(
        json.dumps({"exp": int(exp)}).encode("ascii")
    ).rstrip(b"=").decode("ascii")
    return f"{header}.{payload}.sig"


class _FakeClock:
    """Deterministic wall clock; base 10 keeps Dhan's TOTP window phase safe."""

    def __init__(self, base: float = 10.0) -> None:
        self._now = base

    def __call__(self) -> float:
        return self._now

    def advance(self, seconds: float) -> None:
        self._now += seconds


def _noop_sleep(_seconds: float) -> None:
    """Never sleep — mints stay instant under the fake clock."""


class _ScriptedFetch:
    """Injected fetch routing mint POSTs vs authenticated API calls.

    Counts mint attempts, records the auth token seen on each API call, and
    can 401-reject exactly one API call to drive the ``on_auth_failure``
    mint path.
    """

    def __init__(
        self,
        *,
        mint_marker: str,
        account_marker: str,
        mint_response: dict[str, object],
        account_response: dict[str, object],
        token_header: str,
        bearer_prefix: bool = False,
        reject_once: bool = False,
    ) -> None:
        self._mint_marker = mint_marker
        self._account_marker = account_marker
        self._mint_response = mint_response
        self._account_response = account_response
        self._token_header = token_header
        self._bearer_prefix = bearer_prefix
        self._reject_once = reject_once
        self.mint_calls = 0
        self.account_calls = 0
        self.wire_tokens: list[str] = []

    def __call__(self, method: str, url: str, **kwargs: object) -> tuple[int, object]:
        if self._mint_marker in url:
            self.mint_calls += 1
            return 200, dict(self._mint_response)
        if self._account_marker in url:
            self.account_calls += 1
            headers = dict(kwargs.get("headers") or {})
            token = str(headers.get(self._token_header, ""))
            if self._bearer_prefix and token.startswith("Bearer "):
                token = token[len("Bearer ") :]
            self.wire_tokens.append(token)
            if self._reject_once and self.account_calls == 1:
                return 401, {"message": "Invalid Token"}
            return 200, dict(self._account_response)
        return 200, {}


def _seed_token_state(path: Path, *, token: str, expires_at: float) -> None:
    """Write a durable token state file in the exact ``_save`` shape."""
    path.write_text(
        json.dumps(
            {
                "generation": 1,
                "token": token,
                "issued_at": "2026-01-01T00:00:00+00:00",
                "expires_at": expires_at,
                "refresh_token": None,
                "access_token": token,
                "expires_at_ms": int(expires_at * 1000),
            }
        )
    )
    path.with_name(path.name + ".generation").write_text("1")


# ---------------------------------------------------------------------------
# Dhan live token path
# ---------------------------------------------------------------------------


def _dhan_broker(
    tmp_path: Path,
    monkeypatch,
    fetch: _ScriptedFetch,
    clock: _FakeClock,
    *,
    seed_token: str | None = None,
    seed_expires_at: float | None = None,
):
    token_path = tmp_path / "dhan-token-state.json"
    monkeypatch.setenv("TRADEX_RUNTIME_DIR", str(tmp_path))
    monkeypatch.setenv("DHAN_CLIENT_ID", "test-client")
    monkeypatch.setenv("DHAN_PIN", "1234")
    monkeypatch.setenv("DHAN_TOTP_SECRET", _TOTP_SECRET)
    monkeypatch.setenv("DHAN_TOKEN_PATH", str(token_path))
    if seed_token is not None:
        assert seed_expires_at is not None
        _seed_token_state(token_path, token=seed_token, expires_at=seed_expires_at)
    cooldown = TotpCooldownGuard(
        cooldown_seconds=0.0, broker="dhan", state_path=tmp_path / "dhan-cooldown.json"
    )
    broker = build_dhan_from_env(
        fetch=fetch,
        cooldown=cooldown,
        token_clock=clock,
        token_sleeper=_noop_sleep,
        load_instruments=False,
    )
    return broker, token_path


def _dhan_fetch(*, reject_once: bool = False) -> _ScriptedFetch:
    return _ScriptedFetch(
        mint_marker="auth.dhan.co",
        account_marker="fundlimit",
        mint_response={"accessToken": "mint-token-1"},
        account_response={"data": {"availableBalance": "50000"}},
        token_header="access-token",
        reject_once=reject_once,
    )


def _dhan_mint_jwt_fetch(*, reject_once: bool = False) -> _ScriptedFetch:
    """Dhan mint that returns a real JWT (with exp) like production does."""
    return _ScriptedFetch(
        mint_marker="auth.dhan.co",
        account_marker="fundlimit",
        mint_response={"accessToken": _fake_jwt(3600.0)},
        account_response={"data": {"availableBalance": "50000"}},
        token_header="access-token",
        reject_once=reject_once,
    )


class TestDhanLiveTokenPath:
    """Dhan live token path: connect never mints; mints only on demand."""

    def test_connect_never_mints_with_valid_persisted_token(self, tmp_path, monkeypatch) -> None:
        clock = _FakeClock()
        fetch = _dhan_fetch()
        broker, token_path = _dhan_broker(
            tmp_path, monkeypatch, fetch, clock,
            seed_token="seeded-token", seed_expires_at=clock() + 3600.0,
        )
        try:
            broker.connect()
            assert fetch.mint_calls == 0  # connect must not mint

            account = broker.get_account()  # live auth call reuses persisted token
            assert account.balance.amount == Decimal("50000")
            assert fetch.mint_calls == 0  # valid persisted token → no mint
            assert fetch.wire_tokens == ["seeded-token"]  # seeded token on the wire
        finally:
            broker.close()

    def test_connect_never_mints_without_any_token(self, tmp_path, monkeypatch) -> None:
        clock = _FakeClock()
        fetch = _dhan_fetch()
        broker, token_path = _dhan_broker(tmp_path, monkeypatch, fetch, clock)
        try:
            broker.connect()
            assert fetch.mint_calls == 0  # even a missing token is not minted on connect
        finally:
            broker.close()

    def test_missing_token_mints_once_on_first_use(self, tmp_path, monkeypatch) -> None:
        clock = _FakeClock()
        fetch = _dhan_fetch()
        broker, token_path = _dhan_broker(tmp_path, monkeypatch, fetch, clock)
        try:
            broker.connect()
            account = broker.get_account()  # no persisted token → mint exactly once
            assert account.balance.amount == Decimal("50000")
            assert fetch.mint_calls == 1
            assert json.loads(token_path.read_text())["token"] == "mint-token-1"

            broker.get_account()  # minted token trusted → reuse, no re-mint
            assert fetch.mint_calls == 1
        finally:
            broker.close()

    def test_expired_token_mints_once_after_clock_advance(self, tmp_path, monkeypatch) -> None:
        clock = _FakeClock()
        fetch = _dhan_fetch()
        broker, token_path = _dhan_broker(
            tmp_path, monkeypatch, fetch, clock,
            seed_token="seeded-token", seed_expires_at=clock() + 3600.0,
        )
        try:
            broker.connect()
            broker.get_account()  # still valid → reuse
            assert fetch.mint_calls == 0
            assert fetch.wire_tokens == ["seeded-token"]

            clock.advance(3600.0)  # fake clock moves past expiry + 10-min buffer
            account = broker.get_account()  # expired → mint exactly once
            assert account.balance.amount == Decimal("50000")
            assert fetch.mint_calls == 1
            assert fetch.wire_tokens == ["seeded-token", "mint-token-1"]
            assert json.loads(token_path.read_text())["token"] == "mint-token-1"

            broker.get_account()  # fresh token trusted → no second mint
            assert fetch.mint_calls == 1
        finally:
            broker.close()

    def test_401_rejected_token_mints_once_and_retries(self, tmp_path, monkeypatch) -> None:
        clock = _FakeClock()
        fetch = _dhan_fetch(reject_once=True)
        broker, token_path = _dhan_broker(
            tmp_path, monkeypatch, fetch, clock,
            seed_token="seeded-token", seed_expires_at=clock() + 3600.0,
        )
        try:
            broker.connect()
            account = broker.get_account()  # 401 → on_auth_failure → mint → retry
            assert account.balance.amount == Decimal("50000")
            assert fetch.mint_calls == 1  # 401-once: exactly one mint
            assert fetch.wire_tokens == ["seeded-token", "mint-token-1"]
            assert json.loads(token_path.read_text())["token"] == "mint-token-1"
            assert broker.get_account().balance.amount == Decimal("50000")  # fresh reused
            assert fetch.mint_calls == 1
        finally:
            broker.close()

    def test_fresh_build_reuses_minted_token_without_minting(self, tmp_path, monkeypatch) -> None:
        """A second broker over the same state file never re-mints the token."""
        clock = _FakeClock()
        fetch = _dhan_fetch()
        broker, token_path = _dhan_broker(tmp_path, monkeypatch, fetch, clock)
        try:
            broker.connect()
            broker.get_account()  # no persisted token → mint once
            assert fetch.mint_calls == 1
        finally:
            broker.close()

        # Rebuild over the same (now populated) state file.
        fresh_fetch = _dhan_fetch()
        broker2, _ = _dhan_broker(tmp_path, monkeypatch, fresh_fetch, clock)
        try:
            broker2.connect()
            assert fresh_fetch.mint_calls == 0  # persisted token reused on connect
            broker2.get_account()
            assert fresh_fetch.mint_calls == 0  # …and on authenticated calls
            assert fresh_fetch.wire_tokens == ["mint-token-1"]
        finally:
            broker2.close()

    def test_minted_jwt_token_expires_and_mints_again(self, tmp_path, monkeypatch) -> None:
        """A JWT-minted token (with exp) re-mints after the clock passes its expiry."""
        clock = _FakeClock()
        fetch = _dhan_mint_jwt_fetch()
        broker, token_path = _dhan_broker(tmp_path, monkeypatch, fetch, clock)
        try:
            broker.connect()
            broker.get_account()  # no persisted token → mint (JWT exp=3600)
            assert fetch.mint_calls == 1
            assert fetch.wire_tokens == [_fake_jwt(3600.0)]

            clock.advance(3600.0)  # past JWT exp (3600) + 10-min buffer
            broker.get_account()  # minted token now expired → mint again
            assert fetch.mint_calls == 2
            assert fetch.wire_tokens == [_fake_jwt(3600.0), _fake_jwt(3600.0)]
        finally:
            broker.close()


# ---------------------------------------------------------------------------
# Upstox live token path
# ---------------------------------------------------------------------------


def _upstox_broker(
    tmp_path: Path,
    monkeypatch,
    fetch: _ScriptedFetch,
    clock: _FakeClock,
    *,
    seed_token: str | None = None,
    seed_expires_at: float | None = None,
):
    token_path = tmp_path / "upstox-token-state.json"
    monkeypatch.setenv("TRADEX_RUNTIME_DIR", str(tmp_path))
    monkeypatch.setenv("UPSTOX_REFRESH_TOKEN", "rt-env")
    monkeypatch.setenv("UPSTOX_API_KEY", "test-key")
    monkeypatch.setenv("UPSTOX_API_SECRET", "test-secret")
    monkeypatch.setenv("UPSTOX_TOKEN_PATH", str(token_path))
    if seed_token is not None:
        assert seed_expires_at is not None
        _seed_token_state(token_path, token=seed_token, expires_at=seed_expires_at)
    cooldown = TotpCooldownGuard(
        cooldown_seconds=0.0, broker="upstox", state_path=tmp_path / "upstox-cooldown.json"
    )
    broker = build_upstox_from_env(
        fetch=fetch,
        cooldown=cooldown,
        token_clock=clock,
        load_instruments=False,
    )
    return broker, token_path


def _upstox_fetch(*, reject_once: bool = False) -> _ScriptedFetch:
    return _ScriptedFetch(
        mint_marker="login/authorization/token",
        account_marker="get-funds-and-margin",
        mint_response={
            "access_token": "mint-token-1",
            "expires_in": 3600,
            "refresh_token": "rt-minted",
        },
        account_response={"data": {"available_to_trade": {"total": "50000"}}},
        token_header="Authorization",
        bearer_prefix=True,
        reject_once=reject_once,
    )


class TestUpstoxLiveTokenPath:
    """Upstox live token path: connect never mints; mints only on demand."""

    def test_connect_never_mints_with_valid_persisted_token(self, tmp_path, monkeypatch) -> None:
        clock = _FakeClock()
        fetch = _upstox_fetch()
        broker, token_path = _upstox_broker(
            tmp_path, monkeypatch, fetch, clock,
            seed_token="seeded-token", seed_expires_at=clock() + 3600.0,
        )
        try:
            broker.connect()
            assert fetch.mint_calls == 0  # connect must not mint

            account = broker.get_account()
            assert account.balance.amount == Decimal("50000")
            assert fetch.mint_calls == 0  # valid persisted token → no mint
            assert fetch.wire_tokens == ["seeded-token"]
        finally:
            broker.close()

    def test_connect_never_mints_without_any_token(self, tmp_path, monkeypatch) -> None:
        clock = _FakeClock()
        fetch = _upstox_fetch()
        broker, token_path = _upstox_broker(tmp_path, monkeypatch, fetch, clock)
        try:
            broker.connect()
            assert fetch.mint_calls == 0
        finally:
            broker.close()

    def test_missing_token_mints_once_on_first_use(self, tmp_path, monkeypatch) -> None:
        clock = _FakeClock()
        fetch = _upstox_fetch()
        broker, token_path = _upstox_broker(tmp_path, monkeypatch, fetch, clock)
        try:
            broker.connect()
            account = broker.get_account()
            assert account.balance.amount == Decimal("50000")
            assert fetch.mint_calls == 1
            assert json.loads(token_path.read_text())["token"] == "mint-token-1"

            broker.get_account()
            assert fetch.mint_calls == 1
        finally:
            broker.close()

    def test_expired_token_mints_once_after_clock_advance(self, tmp_path, monkeypatch) -> None:
        clock = _FakeClock()
        fetch = _upstox_fetch()
        broker, token_path = _upstox_broker(
            tmp_path, monkeypatch, fetch, clock,
            seed_token="seeded-token", seed_expires_at=clock() + 3600.0,
        )
        try:
            broker.connect()
            broker.get_account()
            assert fetch.mint_calls == 0
            assert fetch.wire_tokens == ["seeded-token"]

            clock.advance(3600.0)
            account = broker.get_account()
            assert account.balance.amount == Decimal("50000")
            assert fetch.mint_calls == 1
            assert fetch.wire_tokens == ["seeded-token", "mint-token-1"]
            assert json.loads(token_path.read_text())["token"] == "mint-token-1"

            broker.get_account()
            assert fetch.mint_calls == 1
        finally:
            broker.close()

    def test_401_rejected_token_mints_once_and_retries(self, tmp_path, monkeypatch) -> None:
        clock = _FakeClock()
        fetch = _upstox_fetch(reject_once=True)
        broker, token_path = _upstox_broker(
            tmp_path, monkeypatch, fetch, clock,
            seed_token="seeded-token", seed_expires_at=clock() + 3600.0,
        )
        try:
            broker.connect()
            account = broker.get_account()  # 401 → on_auth_failure → mint → retry
            assert account.balance.amount == Decimal("50000")
            assert fetch.mint_calls == 1
            assert fetch.wire_tokens == ["seeded-token", "mint-token-1"]
            assert json.loads(token_path.read_text())["token"] == "mint-token-1"
            assert broker.get_account().balance.amount == Decimal("50000")
            assert fetch.mint_calls == 1
        finally:
            broker.close()

    def test_fresh_build_reuses_minted_token_without_minting(self, tmp_path, monkeypatch) -> None:
        """A second broker over the same state file never re-mints the token."""
        clock = _FakeClock()
        fetch = _upstox_fetch()
        broker, token_path = _upstox_broker(tmp_path, monkeypatch, fetch, clock)
        try:
            broker.connect()
            broker.get_account()  # no persisted token → mint once
            assert fetch.mint_calls == 1
        finally:
            broker.close()

        # Rebuild over the same (now populated) state file.
        fresh_fetch = _upstox_fetch()
        broker2, _ = _upstox_broker(tmp_path, monkeypatch, fresh_fetch, clock)
        try:
            broker2.connect()
            assert fresh_fetch.mint_calls == 0  # persisted token reused on connect
            broker2.get_account()
            assert fresh_fetch.mint_calls == 0  # …and on authenticated calls
            assert fresh_fetch.wire_tokens == ["mint-token-1"]
        finally:
            broker2.close()
