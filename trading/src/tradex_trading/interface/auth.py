"""HTTP authentication — a single API key checked via the ``X-API-Key`` header.

The :func:`verify_api_key` factory is parameterised by the app so it can
read the configured key from ``app.state.api_key`` at request time
(rather than capture it at app-build time, which would force a rebuild
on rotation).

Lifted out of ``fastapi_app`` so routes can be unit-tested with a stub
verifier without importing the whole app factory.
"""

from __future__ import annotations

from typing import Any

from fastapi import HTTPException, Security
from fastapi.security import APIKeyHeader

#: Single shared ``APIKeyHeader`` instance. ``auto_error=False`` so the
#: missing-header case is treated as ``None`` (and the verifier can decide
#: whether the no-header case is allowed).
api_key_header = APIKeyHeader(name="X-API-Key", auto_error=False)


def make_verify_api_key(get_state: Any) -> Any:
    """Build a FastAPI dependency that checks ``X-API-Key`` against app state.

    Args:
        get_state: Zero-arg callable returning the FastAPI ``app.state``
            (or any object exposing ``.api_key``). Indirected so the
            dependency can be bound at module level while still reading
            the live state per request.

    Returns:
        Async dependency: raises ``HTTPException(403)`` on mismatch, no-ops
        when no API key is configured.
    """

    async def verify_api_key(api_key: str | None = Security(api_key_header)) -> None:
        expected = get_state().api_key
        if expected is None:
            return  # No auth configured
        if api_key != expected:
            raise HTTPException(status_code=403, detail="Invalid API key")

    return verify_api_key


__all__ = [
    "api_key_header",
    "make_verify_api_key",
]
