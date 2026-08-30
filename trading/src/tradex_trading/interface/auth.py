"""HTTP authentication — a single API key checked via the ``X-API-Key`` header.

:func:`verify_api_key` reads the configured key from ``app.state.api_key``
at request time (rather than capturing it at app-build time, which would
force a rebuild on rotation).

Lifted out of ``fastapi_app`` so routes can be unit-tested with a stub
verifier without importing the whole app factory.
"""

from __future__ import annotations

import secrets

from fastapi import HTTPException, Request


def verify_api_key(request: Request) -> None:
    """FastAPI dependency: require the correct ``X-API-Key`` header.

    Reads ``request.app.state.api_key`` (None => no auth configured, allow).
    Raises ``403`` on missing or wrong key. Constant-time via
    ``secrets.compare_digest`` so a timing oracle never leaks the key.
    """
    expected = getattr(request.app.state, "api_key", None)
    if expected is None:
        return  # No auth configured (and permitted)
    provided = request.headers.get("X-API-Key")
    if not isinstance(provided, str) or not isinstance(expected, str) \
            or not secrets.compare_digest(provided, expected):
        raise HTTPException(status_code=403, detail="Invalid API key")


__all__ = ["verify_api_key"]
