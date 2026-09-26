"""Per-identity sliding-window rate limiter for money commands (order mutations).

Implements ``rate_limit_money`` — a FastAPI dependency that enforces a
per-identity request cap on unsafe HTTP methods (POST/PUT/DELETE/PATCH).
Only money-command routes (orders) need this; it is wired at the include_router
level in fastapi_app.py so orders.py (owned by C2) is not touched.

Identity resolution order (mirrors audit_identity):
  1. Session cookie → ``session:<subject>``
  2. X-API-Key header → ``api_key``
  3. Client IP → ``ip:<host>``  (anonymous / dev / paper mode)

Storage: in-memory deque per identity key; one ``InMemoryRateLimiter`` instance
lives on ``app.state.rate_limiter`` (wired in create_app).

ponytail: process-local; ceiling is a single Uvicorn worker. Upgrade path:
swap ``InMemoryRateLimiter`` for a Redis/shared-backend implementation behind
the same ``check(key) → int | None`` interface.
"""
from __future__ import annotations

import secrets
import threading
import time
from collections import deque

from fastapi import HTTPException, Request

# ---------------------------------------------------------------------------
# Tunables (defaults; callers may construct InMemoryRateLimiter with overrides)
# ---------------------------------------------------------------------------

_DEFAULT_MAX = 20           # mutations per window per identity
_DEFAULT_WINDOW = 60        # seconds

_UNSAFE_METHODS = frozenset({"POST", "PUT", "DELETE", "PATCH"})


# ---------------------------------------------------------------------------
# Rate-limiter store
# ---------------------------------------------------------------------------


class InMemoryRateLimiter:
    """Thread-safe sliding-window rate limiter.

    One instance lives on ``app.state.rate_limiter``; each test-client app
    gets its own instance so tests are isolated.

    Args:
        max_requests: Maximum allowed mutations per ``window_seconds``.
        window_seconds: Width of the sliding window in seconds.
    """

    def __init__(
        self,
        max_requests: int = _DEFAULT_MAX,
        window_seconds: int = _DEFAULT_WINDOW,
    ) -> None:
        self._max = max_requests
        self._window = window_seconds
        self._lock = threading.Lock()
        self._buckets: dict[str, deque[float]] = {}

    def check(self, key: str) -> int | None:
        """Record a request attempt for ``key``.

        Returns:
            ``None``   — within limit; request is allowed.
            ``int``    — seconds the caller must wait; request is denied.
        """
        now = time.monotonic()
        cutoff = now - self._window
        with self._lock:
            if key not in self._buckets:
                self._buckets[key] = deque()
            bucket = self._buckets[key]
            # Evict timestamps that have fallen outside the window.
            while bucket and bucket[0] < cutoff:
                bucket.popleft()
            if len(bucket) >= self._max:
                # Oldest entry in the current window; caller must wait until it
                # falls out of the window before a new slot opens.
                retry_after = int(self._window - (now - bucket[0])) + 1
                return max(retry_after, 1)
            bucket.append(now)
            return None

    def reset(self) -> None:
        """Clear all buckets. Useful for test teardown."""
        with self._lock:
            self._buckets.clear()


# ---------------------------------------------------------------------------
# Identity derivation
# ---------------------------------------------------------------------------


def _identity_key(request: Request) -> str:
    """Derive a rate-limit identity key from the request.

    Replicates the resolution logic of ``audit_identity`` without importing
    it to avoid a dependency cycle. Reads app.state directly.
    """
    store = getattr(request.app.state, "session_store", None)
    if store is not None:
        raw_id = request.cookies.get("tradex_session")
        if raw_id:
            record = store.lookup(raw_id)
            if record is not None:
                return f"session:{record.subject}"

    expected = getattr(request.app.state, "api_key", None)
    if expected is not None:
        provided = request.headers.get("X-API-Key", "")
        if provided and secrets.compare_digest(str(provided), str(expected)):
            return "api_key"

    client = request.client
    return f"ip:{client.host if client else 'unknown'}"


# ---------------------------------------------------------------------------
# FastAPI dependency
# ---------------------------------------------------------------------------


def rate_limit_money(request: Request) -> None:
    """FastAPI dependency: sliding-window rate limit for order mutation commands.

    Skipped entirely for safe HTTP methods (GET/HEAD/OPTIONS) — wiring at the
    include_router level means GET /orders also receives this dep, but the
    method guard makes it a no-op there.

    When ``app.state.rate_limiter`` is absent (paper/dev mode, or a test app
    that deliberately omits the limiter) the dependency is a no-op.

    Raises:
        HTTPException(429) with ``Retry-After`` header when the identity has
        exceeded its mutation budget for the current window.
    """
    if request.method not in _UNSAFE_METHODS:
        return

    limiter: InMemoryRateLimiter | None = getattr(
        request.app.state, "rate_limiter", None
    )
    if limiter is None:
        return  # no limiter configured; dev / paper mode passes through

    key = _identity_key(request)
    retry_after = limiter.check(key)
    if retry_after is not None:
        raise HTTPException(
            status_code=429,
            detail="rate limit exceeded; too many order mutations",
            headers={"Retry-After": str(retry_after)},
        )


__all__ = ["InMemoryRateLimiter", "rate_limit_money"]
