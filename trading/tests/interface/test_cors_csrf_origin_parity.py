"""CORS and CSRF must resolve to the SAME browser origin.

Before the unification, ``TRADEX_UI_ORIGIN`` was read twice with different
precedence:

  * ``fastapi_app.create_app`` read the environment only, and the value was
    frozen into the CORS allow-list at middleware-registration time;
  * ``auth.deps.require_csrf_if_session`` read ``app.state.ui_origin`` first
    and the environment second, per request.

A caller that set ``app.state.ui_origin`` programmatically (no env var) got
CSRF enforcing one origin and CORS allowing a different one. These tests pin
the unified policy across all four precedence combinations, and pin the
fail-closed property that matters for a security boundary: an origin that is
not the resolved one is rejected by BOTH consumers.
"""
from __future__ import annotations

from typing import Any
from unittest.mock import MagicMock

import pytest

fastapi = pytest.importorskip("fastapi")
from fastapi.testclient import TestClient  # noqa: E402

from tradex_interfaces.auth.origin import (  # noqa: E402
    DEFAULT_UI_ORIGIN,
    UI_ORIGIN_ENV_VAR,
    resolve_ui_origin,
)
from tradex_interfaces.fastapi_app import create_app  # noqa: E402

# Mirrors the _make_app helper in test_csrf_enforcement.py.
def _make_app(api_key: str | None = "test-key") -> Any:
    session = MagicMock()
    session.state = "READY"
    session.mode = "paper"
    session.engine = MagicMock()
    session.engine.all_orders.return_value = []
    return create_app(session=session, api_key=api_key)


def _cors_allows(client: TestClient, origin: str) -> bool:
    """True when the CORS preflight approves *origin*."""
    r = client.options(
        "/orders",
        headers={"Origin": origin, "Access-Control-Request-Method": "GET"},
    )
    return (
        r.status_code == 200
        and r.headers.get("access-control-allow-origin") == origin
    )


def _cors_allow_list(app: Any) -> list[str]:
    """The origin list the CORS middleware was actually built with."""
    # Building the stack is what freezes the allow-list; do it explicitly so
    # the assertion does not depend on a prior request having happened.
    app.build_middleware_stack()
    middleware = next(
        m for m in app.user_middleware if m.cls.__name__.endswith("CORSMiddleware")
    )
    built = middleware.cls(app=app, **middleware.kwargs)
    return list(built.allow_origins)


def _csrf_origin(app: Any) -> str:
    """The origin the CSRF guard will enforce for this app."""
    return resolve_ui_origin(app.state)


# ---------------------------------------------------------------------------
# THE BUG: app.state.ui_origin set without the env var.
# Pre-fix this asserted the CORS allow-list to be the env/default origin while
# CSRF trusted app.state — i.e. the two policies disagreed.
# ---------------------------------------------------------------------------


def test_app_state_origin_without_env_var_drives_cors_and_csrf(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.delenv(UI_ORIGIN_ENV_VAR, raising=False)

    app = _make_app()
    app.state.ui_origin = "http://app-state.example:9000"

    assert _cors_allow_list(app) == ["http://app-state.example:9000"]
    assert _csrf_origin(app) == "http://app-state.example:9000"

    client = TestClient(app)
    assert _cors_allows(client, "http://app-state.example:9000")
    assert not _cors_allows(client, DEFAULT_UI_ORIGIN)


def test_app_state_origin_without_env_var_passes_csrf_origin_check(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """End to end: a session-authenticated POST with the app.state origin and
    a valid CSRF token must not be rejected on Origin."""
    monkeypatch.delenv(UI_ORIGIN_ENV_VAR, raising=False)

    app = _make_app()
    app.state.ui_origin = "http://app-state.example:9000"
    client = TestClient(app)
    login = client.post("/auth/login", json={"password": "test-key"})
    assert login.status_code == 200

    r = client.post(
        "/orders",
        json={"instrument_id": "NSE:RELIANCE", "side": "BUY", "quantity": 1},
        headers={
            "X-CSRF-Token": login.json()["csrf_token"],
            "Origin": "http://app-state.example:9000",
            "Idempotency-Key": "k-app-state-origin",
        },
    )
    assert r.status_code != 403

    # ...and the default origin, which CORS no longer allows, is rejected.
    r = client.post(
        "/orders",
        json={"instrument_id": "NSE:RELIANCE", "side": "BUY", "quantity": 1},
        headers={
            "X-CSRF-Token": login.json()["csrf_token"],
            "Origin": DEFAULT_UI_ORIGIN,
            "Idempotency-Key": "k-default-origin",
        },
    )
    assert r.status_code == 403


# ---------------------------------------------------------------------------
# Env var set, no app.state override.
# ---------------------------------------------------------------------------


def test_env_var_without_app_state_drives_cors_and_csrf(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv(UI_ORIGIN_ENV_VAR, "http://env.example:4000")

    app = _make_app()

    assert _cors_allow_list(app) == ["http://env.example:4000"]
    assert _csrf_origin(app) == "http://env.example:4000"
    assert app.state.ui_origin == "http://env.example:4000"

    client = TestClient(app)
    assert _cors_allows(client, "http://env.example:4000")
    assert not _cors_allows(client, DEFAULT_UI_ORIGIN)


# ---------------------------------------------------------------------------
# Neither set -> documented default, in both places.
# ---------------------------------------------------------------------------


def test_default_used_by_cors_and_csrf_when_nothing_configured(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.delenv(UI_ORIGIN_ENV_VAR, raising=False)

    app = _make_app()

    assert _cors_allow_list(app) == [DEFAULT_UI_ORIGIN]
    assert _csrf_origin(app) == DEFAULT_UI_ORIGIN
    assert app.state.ui_origin == DEFAULT_UI_ORIGIN
    assert DEFAULT_UI_ORIGIN == "http://localhost:5173"


# ---------------------------------------------------------------------------
# Both set -> app.state wins in both places (pre-existing CSRF behaviour).
# ---------------------------------------------------------------------------


def test_app_state_wins_over_env_var_in_cors_and_csrf(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv(UI_ORIGIN_ENV_VAR, "http://env.example:4000")

    app = _make_app()
    app.state.ui_origin = "http://app-state.example:9000"

    assert _cors_allow_list(app) == ["http://app-state.example:9000"]
    assert _csrf_origin(app) == "http://app-state.example:9000"

    client = TestClient(app)
    assert _cors_allows(client, "http://app-state.example:9000")
    assert not _cors_allows(client, "http://env.example:4000")


# ---------------------------------------------------------------------------
# Resolver contract
# ---------------------------------------------------------------------------


def test_resolver_precedence_is_state_then_env_then_default(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    class _State:
        ui_origin = "http://app-state.example:9000"

    monkeypatch.setenv(UI_ORIGIN_ENV_VAR, "http://env.example:4000")
    assert resolve_ui_origin(_State()) == "http://app-state.example:9000"
    assert resolve_ui_origin(object()) == "http://env.example:4000"
    assert resolve_ui_origin(None) == "http://env.example:4000"

    monkeypatch.delenv(UI_ORIGIN_ENV_VAR, raising=False)
    assert resolve_ui_origin(_State()) == "http://app-state.example:9000"
    assert resolve_ui_origin(object()) == DEFAULT_UI_ORIGIN
    assert resolve_ui_origin(None) == DEFAULT_UI_ORIGIN


@pytest.mark.parametrize("junk", [None, "", 0, [], {}, object()])
def test_resolver_ignores_non_string_and_empty_values(
    monkeypatch: pytest.MonkeyPatch, junk: object
) -> None:
    """Non-strings and empty strings fall through, matching the old
    ``getattr(...) or os.environ.get(...) or DEFAULT`` chain exactly — so
    unification cannot widen what CSRF accepts."""

    class _State:
        ui_origin = junk

    monkeypatch.setenv(UI_ORIGIN_ENV_VAR, "http://env.example:4000")
    assert resolve_ui_origin(_State()) == "http://env.example:4000"

    monkeypatch.setenv(UI_ORIGIN_ENV_VAR, "")
    assert resolve_ui_origin(_State()) == DEFAULT_UI_ORIGIN


def test_resolver_never_returns_wildcard(monkeypatch: pytest.MonkeyPatch) -> None:
    """A wildcard config value must never reach CORS as allow-all.

    Starlette reads ``allow_origins=["*"]`` as "allow every origin", so
    honouring a "*" would silently disable the origin lock. The resolver
    refuses it and falls through to the next precedence level instead.
    """

    class _WildcardState:
        ui_origin = "*"

    monkeypatch.delenv(UI_ORIGIN_ENV_VAR, raising=False)
    assert resolve_ui_origin(_WildcardState()) == DEFAULT_UI_ORIGIN

    # With an env var present, the rejected state value falls through to it.
    monkeypatch.setenv(UI_ORIGIN_ENV_VAR, "http://env.example:4000")
    assert resolve_ui_origin(_WildcardState()) == "http://env.example:4000"

    # And end to end: a "*" on app.state never becomes allow-all in CORS.
    app = _make_app()
    app.state.ui_origin = "*"
    assert _cors_allow_list(app) == ["http://env.example:4000"]
    assert not _cors_allow_list(app) == ["*"]

    client = TestClient(app)
    assert not _cors_allows(client, "https://evil.example.com")
    assert not _cors_allows(client, "*")
    assert _cors_allows(client, "http://env.example:4000")


# ---------------------------------------------------------------------------
# The two consumers must never disagree, whatever the configuration.
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("env", "state", "expected"),
    [
        (None, None, DEFAULT_UI_ORIGIN),
        ("http://env.example:4000", None, "http://env.example:4000"),
        (None, "http://app-state.example:9000", "http://app-state.example:9000"),
        (
            "http://env.example:4000",
            "http://app-state.example:9000",
            "http://app-state.example:9000",
        ),
    ],
)
def test_cors_and_csrf_never_disagree(
    monkeypatch: pytest.MonkeyPatch,
    env: str | None,
    state: str | None,
    expected: str,
) -> None:
    if env is None:
        monkeypatch.delenv(UI_ORIGIN_ENV_VAR, raising=False)
    else:
        monkeypatch.setenv(UI_ORIGIN_ENV_VAR, env)

    app = _make_app()
    if state is not None:
        app.state.ui_origin = state

    assert _cors_allow_list(app) == [expected]
    assert _csrf_origin(app) == expected
    assert app.state.ui_origin == expected
