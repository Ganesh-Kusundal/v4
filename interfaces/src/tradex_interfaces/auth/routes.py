"""Authentication endpoints: session bootstrap, CSRF token, login, logout.

Routes:
  GET  /auth/session   — non-secret session metadata (safe to poll; no mutation)
  POST /auth/login     — issue an HttpOnly session cookie; returns CSRF token
  POST /auth/logout    — revoke current session; clear cookie

Migration note (Section 10 of the design):
  During the transition window ``POST /auth/login`` accepts the configured
  API key as the password.  This is the bounded compatibility path; the UI
  must not display or re-use the key — it receives only the opaque session
  cookie and a CSRF token.
"""
from __future__ import annotations

import logging
import secrets
from typing import Any

from fastapi import APIRouter, HTTPException, Request, Response

from .session_store import InMemorySessionStore, SESSION_ABSOLUTE_TTL

log = logging.getLogger(__name__)

#: Cookie name for HTTP loopback development (no ``__Host-`` prefix and no
#: ``Secure`` flag, which is required for HTTPS-only ``__Host-`` cookies).
#: A production TLS deployment should use ``__Host-tradex_session`` with
#: ``secure=True`` — that transition is a deployment-config change, not a
#: code change, because the validation logic in deps.py reads by this name.
_COOKIE_NAME = "tradex_session"

router = APIRouter(prefix="/auth", tags=["auth"])


def _set_session_cookie(response: Response, raw_id: str) -> None:
    """Write the session cookie.  Never set Secure here — callers that are on
    HTTPS must set it via a deployment reverse-proxy or a config flag (not yet
    plumbed; ponytail: HTTP loopback only for now).
    """
    response.set_cookie(
        key=_COOKIE_NAME,
        value=raw_id,
        httponly=True,
        samesite="lax",
        path="/",
        max_age=int(SESSION_ABSOLUTE_TTL.total_seconds()),
        secure=False,   # ponytail: loopback HTTP; upgrade to True for HTTPS
    )


def _clear_session_cookie(response: Response) -> None:
    response.delete_cookie(key=_COOKIE_NAME, path="/", httponly=True, samesite="lax")


# ---------------------------------------------------------------------------
# Routes
# ---------------------------------------------------------------------------


@router.get("/session")
async def get_session(request: Request) -> dict[str, Any]:
    """Return non-secret session metadata.

    Safe to call on every page load or tab focus.  The response body never
    contains the raw session ID, API key, or CSRF secret.

    When authenticated the response includes a ``csrf_token`` safe to hold
    in JavaScript memory (not localStorage/sessionStorage) for use as
    ``X-CSRF-Token`` on mutations.
    """
    store: InMemorySessionStore | None = getattr(
        request.app.state, "session_store", None
    )
    if store is None:
        return {"authenticated": False, "session_auth_available": False}

    raw_id = request.cookies.get(_COOKIE_NAME)
    if not raw_id:
        return {"authenticated": False, "session_auth_available": True}

    record = store.lookup(raw_id)
    if record is None:
        return {"authenticated": False, "session_auth_available": True}

    from .csrf import make_csrf_token

    return {
        "authenticated": True,
        "subject": record.subject,
        "csrf_token": make_csrf_token(record.csrf_secret),
        "session_auth_available": True,
    }


@router.post("/login")
async def login(request: Request, response: Response) -> dict[str, Any]:
    """Bootstrap a server-side session.

    Accepts ``{"password": "..."}`` in the request body.  During the
    migration window the password is checked against the configured API key
    (``app.state.api_key``).  A successful login:
      - creates a fresh server-side session;
      - writes an HttpOnly session cookie;
      - returns a CSRF token for immediate client use.

    The API key itself is never echoed in the response.
    """
    store: InMemorySessionStore | None = getattr(
        request.app.state, "session_store", None
    )
    if store is None:
        raise HTTPException(status_code=503, detail="Session auth not available")

    try:
        body = await request.json()
    except Exception:
        raise HTTPException(status_code=400, detail="Invalid JSON body")

    password: str = body.get("password", "") if isinstance(body, dict) else ""
    expected_key: str | None = getattr(request.app.state, "api_key", None)

    if not expected_key:
        raise HTTPException(
            status_code=503, detail="No login credentials configured"
        )

    if not password or not secrets.compare_digest(str(password), str(expected_key)):
        log.warning("auth.login: failed attempt from %s", request.client)
        raise HTTPException(status_code=401, detail="Invalid credentials")

    raw_id, record = store.create(subject="operator")
    _set_session_cookie(response, raw_id)

    from .csrf import make_csrf_token

    log.info("auth.login: session created subject=%s", record.subject)
    return {
        "authenticated": True,
        "subject": record.subject,
        "csrf_token": make_csrf_token(record.csrf_secret),
    }


@router.post("/logout")
async def logout(request: Request, response: Response) -> dict[str, Any]:
    """Revoke the current session and clear the session cookie.

    Idempotent: always clears the cookie regardless of whether a valid session
    was found.  A revoked session is immediately invalid for HTTP and WebSocket.
    """
    store: InMemorySessionStore | None = getattr(
        request.app.state, "session_store", None
    )
    raw_id = request.cookies.get(_COOKIE_NAME)
    if store is not None and raw_id:
        revoked = store.revoke(raw_id)
        if revoked:
            log.info("auth.logout: session revoked")

    _clear_session_cookie(response)
    return {"logged_out": True}


__all__ = ["router"]
