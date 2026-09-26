"""FastAPI dependencies for session-based authentication and CSRF protection.

Two credential paths coexist (migration policy, Section 10 of the design):

  Session cookie path   Browser clients authenticate via POST /auth/login,
                        receive an HttpOnly session cookie, and send it on
                        every request. State-changing requests additionally
                        require a CSRF token obtained from GET /auth/session.

  Legacy API-key path   Programmatic / loopback-dev clients send X-API-Key.
                        No CSRF enforcement applies — the shared key IS the
                        cross-request credential, not a cookie vulnerable to
                        CSRF.

When neither credential source is configured (paper/dev mode), every request
is allowed through without challenge.  The full fail-closed policy is a startup
constraint (``_require_api_key_for_live``) rather than a per-request gate.
"""
from __future__ import annotations

import logging
import secrets

from fastapi import HTTPException, Request

from .origin import resolve_ui_origin
from .session_store import InMemorySessionStore, SessionRecord

log = logging.getLogger(__name__)

_COOKIE_NAME = "tradex_session"
_UNSAFE_METHODS = frozenset({"POST", "PUT", "DELETE", "PATCH"})

#: Request header carrying the legacy programmatic API key.  A wire contract
#: shared with external clients and the browser — rename, never retype.
_API_KEY_HEADER = "X-API-Key"


# ---------------------------------------------------------------------------
# Internal helpers
# ---------------------------------------------------------------------------


def _store(request: Request) -> InMemorySessionStore | None:
    """Return the session store attached to the app, or None."""
    return getattr(request.app.state, "session_store", None)


def _cookie_session(
    request: Request, store: InMemorySessionStore
) -> SessionRecord | None:
    """Resolve session from the HttpOnly cookie. Returns None when absent/invalid."""
    raw_id = request.cookies.get(_COOKIE_NAME)
    if not raw_id:
        return None
    return store.lookup(raw_id)


# ---------------------------------------------------------------------------
# Public dependencies
# ---------------------------------------------------------------------------


def resolve_session(request: Request) -> SessionRecord | None:
    """Non-raising dependency: return the active session or None."""
    store = _store(request)
    if store is None:
        return None
    return _cookie_session(request, store)


def audit_identity(request: Request) -> str:
    """Return an identity token for structured audit logging.

    Returns:
      ``'session:<subject>'`` for session-cookie-authenticated requests,
      ``'api_key'``           for legacy X-API-Key requests,
      ``'anonymous'``         otherwise (dev / paper mode).
    """
    store = _store(request)
    if store is not None:
        record = _cookie_session(request, store)
        if record is not None:
            return f"session:{record.subject}"
    expected = getattr(request.app.state, "api_key", None)
    if expected is not None:
        provided = request.headers.get(_API_KEY_HEADER)
        if provided and secrets.compare_digest(str(provided), str(expected)):
            return "api_key"
    return "anonymous"


def require_auth(request: Request) -> SessionRecord | None:
    """FastAPI dependency: ensure the request carries valid credentials.

    Evaluation order:
      1. Session cookie  → authenticated; returns ``SessionRecord``
      2. X-API-Key       → authenticated (legacy); returns ``None``
      3. No auth configured (paper/dev) → allowed; returns ``None``
      4. Auth configured but not matched → ``401``/``403``

    Returns:
      ``SessionRecord`` when authenticated via session cookie.
      ``None`` when authenticated via API key or no auth configured.

    Raises:
      ``HTTPException(403)`` when the API key is configured but wrong.
    """
    store = _store(request)
    expected_key = getattr(request.app.state, "api_key", None)

    # --- session cookie path ---
    if store is not None:
        record = _cookie_session(request, store)
        if record is not None:
            return record

    # --- legacy API key path ---
    if expected_key is not None:
        provided = request.headers.get(_API_KEY_HEADER)
        if provided and secrets.compare_digest(str(provided), str(expected_key)):
            return None  # authenticated; no session record
        # Key is configured but didn't match → fail closed
        raise HTTPException(status_code=403, detail="Invalid API key")

    # --- dev/paper: no auth configured ---
    return None


def require_csrf_if_session(request: Request) -> None:
    """CSRF guard for unsafe methods authenticated via session cookie.

    Skipped entirely when:
      - the request method is safe (GET/HEAD/OPTIONS)
      - no session store is configured on the app
      - the request is NOT authenticated via session cookie

    When the request carries a valid session cookie, enforces:
      - ``Origin`` header must equal the configured UI origin
      - ``Referer`` (when present) must start with the UI origin
      - ``X-CSRF-Token`` must match the session's CSRF secret

    A failed token is a ``403``, not a retryable condition.
    """
    if request.method not in _UNSAFE_METHODS:
        return

    store = _store(request)
    if store is None:
        return  # session auth not enabled; CSRF not applicable

    record = _cookie_session(request, store)
    if record is None:
        return  # not session-authenticated; skip CSRF (API key callers exempt)

    # -- Origin enforcement --
    # Same resolver the CORS allow-list is built from, so the trusted browser
    # origin is identical in both places for the same inputs.
    ui_origin: str = resolve_ui_origin(request.app.state)
    origin = request.headers.get("origin")
    if origin is not None and origin != ui_origin:
        log.warning(
            "csrf.origin_mismatch origin=%r expected=%r", origin, ui_origin
        )
        raise HTTPException(status_code=403, detail="CSRF: origin mismatch")

    referer = request.headers.get("referer", "")
    if referer and not referer.startswith(ui_origin):
        log.warning(
            "csrf.referer_mismatch referer=%r expected_prefix=%r",
            referer,
            ui_origin,
        )
        raise HTTPException(status_code=403, detail="CSRF: referer mismatch")

    # -- Token enforcement --
    from .csrf import verify_csrf_token

    token = request.headers.get("x-csrf-token", "")
    if not token or not verify_csrf_token(record.csrf_secret, token):
        log.warning(
            "csrf.token_invalid subject=%r path=%r", record.subject, request.url.path
        )
        raise HTTPException(
            status_code=403, detail="CSRF token invalid or missing"
        )


# ---------------------------------------------------------------------------
# Legacy ``verify_api_key`` — kept here so the package is the sole auth
# module; the original ``interface/auth.py`` is replaced by this package.
# ---------------------------------------------------------------------------


def verify_api_key(request: Request) -> None:
    """FastAPI dependency: require the correct ``X-API-Key`` header (legacy path).

    Reads ``request.app.state.api_key`` at request time.
    ``None`` ⟹ no auth configured, allow through.
    Wrong key ⟹ ``403``.  Constant-time via ``secrets.compare_digest``.
    """
    expected = getattr(request.app.state, "api_key", None)
    if expected is None:
        return  # dev / paper mode
    provided = request.headers.get(_API_KEY_HEADER)
    if (
        not isinstance(provided, str)
        or not isinstance(expected, str)
        or not secrets.compare_digest(provided, expected)
    ):
        raise HTTPException(status_code=403, detail="Invalid API key")


__all__ = [
    "audit_identity",
    "require_auth",
    "require_csrf_if_session",
    "resolve_session",
    "verify_api_key",
]
