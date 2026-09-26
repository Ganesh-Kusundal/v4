"""Authentication package for the TradeX v4 HTTP/WebSocket interface.

Replaces the single-file ``interface/auth.py`` with a structured package.
All symbols that were importable from ``tradex_trading.interface.auth``
remain importable from here for backward compatibility.

Sub-modules
-----------
session_store   InMemorySessionStore + SessionRecord
csrf            CSRF token derivation and verification
deps            FastAPI dependencies: require_auth, require_csrf_if_session,
                audit_identity, resolve_session, verify_api_key (legacy)
routes          /auth/* HTTP endpoints (GET /session, POST /login, /logout)
"""
from .csrf import make_csrf_token, verify_csrf_token
from .deps import (
    audit_identity,
    require_auth,
    require_csrf_if_session,
    resolve_session,
    verify_api_key,  # backward-compat: callers of the old interface/auth.py
)
from .rate_limit import InMemoryRateLimiter, rate_limit_money
from .routes import router as auth_router
from .session_store import InMemorySessionStore, SessionRecord

__all__ = [
    # backward-compat (was the only export of the old auth.py)
    "verify_api_key",
    # new auth primitives
    "require_auth",
    "require_csrf_if_session",
    "resolve_session",
    "audit_identity",
    # session store
    "InMemorySessionStore",
    "SessionRecord",
    # csrf
    "make_csrf_token",
    "verify_csrf_token",
    # rate limiter (C5)
    "InMemoryRateLimiter",
    "rate_limit_money",
    # router
    "auth_router",
]
