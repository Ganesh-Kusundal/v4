"""CSRF token generation and verification.

Pattern: synchronized token — the server derives a deterministic, opaque
token from the per-session CSRF secret using HMAC-SHA256. The client reads
it from ``GET /auth/session`` and returns it as ``X-CSRF-Token`` on every
unsafe (POST/PUT/DELETE/PATCH) request.

The token is:
- deterministic for a given session (allows the client to re-read it)
- bound to the session's secret (cannot be guessed without it)
- constant-time compared (no timing oracle)
- rotated when the session is rotated (login, logout)
"""
from __future__ import annotations

import hashlib
import hmac

_PURPOSE = b"csrf-v1"


def make_csrf_token(csrf_secret: str) -> str:
    """Derive the CSRF token from a session's CSRF secret.

    Returns a hex string safe to return in a JSON response body.
    The secret itself is never returned.
    """
    return hmac.new(csrf_secret.encode(), _PURPOSE, hashlib.sha256).hexdigest()


def verify_csrf_token(csrf_secret: str, provided: str) -> bool:
    """Constant-time comparison of the provided token against the expected value."""
    expected = make_csrf_token(csrf_secret)
    return hmac.compare_digest(expected, provided)


__all__ = ["make_csrf_token", "verify_csrf_token"]
