"""Shared HTTP client composing pipeline + auth + transport.

``ProviderHttpClient`` is the main entry point for broker adapters to make
authenticated HTTP calls.  Production binds ``ResiliencePipeline``
(``common.resilience``, via ``client_shared.build_provider_client``), which
routes through an injected ``fetch``; unit tests may bind the lighter
``tests.support.fetch_pipeline.FetchResiliencePipeline`` instead.
"""

from __future__ import annotations

import json
import logging
import threading
from dataclasses import dataclass
from typing import Any, Protocol

from tradex_domain import AuthenticationError

from tradex_brokers.common.cache import ReadCache
from tradex_brokers.common.token_lifecycle import PortTokenManager
from tradex_brokers.common.transport import HttpTransport

log = logging.getLogger(__name__)

_SUCCESS_STATUS = frozenset(range(200, 300))
_SAFE_AUTH_RETRY_METHODS = frozenset({"GET", "HEAD", "OPTIONS"})

# Transport failures that can occur AFTER the request crossed the wire: the
# venue may have accepted the mutation even though no response came back.
_UNCERTAIN_SEND_ERRORS = (OSError,)


@dataclass(frozen=True)
class AuthRetryPolicy:
    """Configuration for automatic auth retry on 401/403."""
    max_retries: int = 1
    retryable_statuses: frozenset[int] = frozenset({401, 403})


class UncertainSubmissionTracker:
    """Correlation-keyed reservations for uncertain mutating calls.

    Mirrors the v3 three-phase reserve/commit/mark_unresolved discipline:
    once a mutating request has been sent, a timeout or 5xx must NOT free the
    key — reconciliation (an order read keyed by correlation id) must confirm
    the outcome before the same correlation id may be resubmitted.
    """

    def __init__(self) -> None:
        self._reserved: set[str] = set()
        self._unresolved: set[str] = set()
        self._lock = threading.Lock()

    def reserve(self, key: str) -> None:
        with self._lock:
            self._reserved.add(key)

    def commit(self, key: str) -> None:
        with self._lock:
            self._reserved.discard(key)

    def mark_unresolved(self, key: str) -> None:
        with self._lock:
            self._reserved.discard(key)
            self._unresolved.add(key)

    def is_unresolved(self, key: str) -> bool:
        with self._lock:
            return key in self._unresolved

    def clear(self, key: str) -> None:
        """Free a key whose outcome was confirmed (resolve via get_order)."""
        with self._lock:
            self._unresolved.discard(key)
            self._reserved.discard(key)


class SendPipeline(Protocol):
    """Minimal ``send`` contract satisfied by any request pipeline.

    Production binds ``ResiliencePipeline``; unit tests bind the lighter
    ``FetchResiliencePipeline`` test seam. This protocol is the sole typing
    surface ``ProviderHttpClient`` needs from its pipeline.
    """

    def send(self, method: str, url: str, **kwargs: Any) -> Any: ...


class ProviderHttpClient:
    """HTTP client composing pipeline + auth + transport.

    Parameters
    ----------
    transport:
        The underlying HTTP transport for making requests.
    pipeline:
        Request pipeline (``send``); production binds
        ``FetchResiliencePipeline``.
    token_manager:
        Optional token manager for injecting ``Authorization`` headers.
    """

    def __init__(
        self,
        transport: HttpTransport,
        pipeline: SendPipeline,
        token_manager: PortTokenManager | None = None,
        *,
        cache_ttl_seconds: float = 0.0,
        cache_max_entries: int = 1024,
        auth_retry_policy: AuthRetryPolicy | None = None,
    ) -> None:
        self._transport = transport
        self._pipeline = pipeline
        self._token_manager = token_manager
        self._uncertain = UncertainSubmissionTracker()
        self._cache_ttl = cache_ttl_seconds
        self._auth_retry = auth_retry_policy
        self._cache: ReadCache | None = (
            ReadCache(max_entries=cache_max_entries) if cache_ttl_seconds > 0 else None
        )
        self.rate_limiter = getattr(pipeline, "_rate_limiter", None)

    # -- helpers ------------------------------------------------------------

    def _inject_auth(self, kwargs: dict[str, Any]) -> tuple[dict[str, Any], str | None]:
        """Inject auth headers; return ``(kwargs, token_sent)``.

        ``token_sent`` is the exact token placed on the wire so a later 401
        replay can report it as rejected — re-resolving at rejection time could
        mint a fresh token and wrongly mark *that* one rejected (double mint).

        Prefers the transport's dynamic ``token_provider``/``auth_headers``
        (v3 parity: the token is re-resolved on every call so mid-session
        refreshes are picked up automatically). Falls back to ``token_manager``
        ``Authorization: Bearer`` injection.
        """
        headers = dict(kwargs.get("headers") or {})
        token_provider = getattr(self._transport, "_token_provider", None)
        auth_headers = getattr(self._transport, "_auth_headers", None)
        if token_provider is not None and auth_headers is not None:
            token = token_provider()
            headers.update(auth_headers(token))
            kwargs["headers"] = headers
            return kwargs, token
        if self._token_manager is None:
            return kwargs, None
        token = self._token_manager.get_token()
        if not token:
            return kwargs, None
        headers.setdefault("Authorization", f"Bearer {token}")
        kwargs["headers"] = headers
        return kwargs, token

    def _build_url(self, path: str) -> str:
        """Build the full URL from the transport's base URL and the path."""
        if path.startswith(("http://", "https://")):
            return path
        base = self._transport._base_url  # noqa: SLF001
        if base:
            return f"{base}/{path.lstrip('/')}"
        return path

    # -- public API ---------------------------------------------------------

    @staticmethod
    def _cache_key(method: str, url: str, kwargs: dict[str, Any]) -> str:
        """Deterministic key from the request shape; headers/tokens excluded."""
        parts: list[str] = [method.upper(), url]
        for name in ("params", "json"):
            value = kwargs.get(name)
            if value is not None:
                parts.append(f"{name}={json.dumps(value, sort_keys=True, default=str)}")
        return "|".join(parts)

    def _should_cache(self, method: str, cache_read: bool | None) -> bool:
        if self._cache is None or cache_read is False:
            return False
        return cache_read is True or method.upper() in {"GET", "HEAD"}

    @staticmethod
    def _is_business_token_rejection(result: dict) -> bool:
        """Check for broker-specific token rejection markers in response body."""
        from tradex_brokers.common.provider_common import (
            _has_token_rejection_marker,
        )
        # Check for error status with token rejection markers
        status = result.get("status")
        if status is not None:
            status_str = str(status).lower()
            if status_str in ("error", "failed", "failure"):
                return _has_token_rejection_marker(str(result))
        # Also check the entire body for markers
        return _has_token_rejection_marker(str(result))

    def request(
        self,
        method: str,
        path: str,
        *,
        cache_read: bool | None = None,
        **kwargs: Any,
    ) -> dict:
        """Send an HTTP request through the resilience pipeline with optional caching.

        Parameters
        ----------
        method:
            HTTP method (GET, POST, PUT, DELETE, …).
        path:
            URL path or full URL.
        cache_read:
            ``True`` to force cache lookup for this read, ``False`` to skip
            caching, ``None`` (default) to cache safe methods only.
        **kwargs:
            Forwarded to the resilience pipeline (headers, json, params, …).
        """
        url = self._build_url(path)
        use_cache = self._should_cache(method, cache_read)
        key = self._cache_key(method, url, kwargs) if use_cache else None
        if key is not None:
            cached = self._cache.get(key)  # type: ignore[union-attr]
            if cached is not None:
                return cached
        kwargs, token_sent = self._inject_auth(kwargs)
        result = self._pipeline.send(method, url, **kwargs)
        # Auth retry on 401/403 (v3 401-once): notify the token provider of the
        # rejection so it mints a fresh generation, then replay exactly once.
        # Restricted to idempotent methods — POST order mutations stay one-shot.
        transport_auth = (
            getattr(self._transport, "_token_provider", None) is not None
            and getattr(self._transport, "_on_auth_failure", None) is not None
        )
        retry_enabled = isinstance(result, dict) and (
            transport_auth
            or (self._auth_retry is not None and self._token_manager is not None)
        )
        if retry_enabled and method.upper() in _SAFE_AUTH_RETRY_METHODS:
            from tradex_brokers.common.provider_common import is_token_rejection_response

            http_status = result.get("_http_status")
            # 401/403 outright, or a business-level rejection smuggled in a 2xx/
            # 400 body (Dhan DH-901/DH-906 "Invalid Token") — v2/v3 parity.
            is_auth_failure = (
                is_token_rejection_response(http_status, result)
                if http_status is not None
                else self._is_business_token_rejection(result)
            )
            if is_auth_failure:
                on_auth_failure = getattr(self._transport, "_on_auth_failure", None)
                if on_auth_failure is not None and token_sent:
                    # Report the exact token that was rejected. A refresh failure
                    # (cooldown, bad TOTP) propagates chained to the original
                    # rejection — replaying with the same dead token only burns
                    # another mint slot (v3 transport parity).
                    try:
                        on_auth_failure(token_sent)
                    except Exception as refresh_exc:  # noqa: BLE001
                        raise AuthenticationError(
                            f"token rejected by broker and refresh failed: {refresh_exc}"
                        ) from refresh_exc
                kwargs, _ = self._inject_auth(kwargs)
                result = self._pipeline.send(method, url, **kwargs)
        if key is not None and isinstance(result, dict):
            self._cache.set(key, result, ttl_seconds=self._cache_ttl)  # type: ignore[union-attr]
        return result

    def get(self, path: str, **kwargs: Any) -> dict:
        """Send a GET request through the resilience pipeline."""
        return self.request("GET", path, **kwargs)

    def post(self, path: str, **kwargs: Any) -> dict:
        """Send a POST request through the resilience pipeline."""
        return self.request("POST", path, **kwargs)

    def put(self, path: str, **kwargs: Any) -> dict:
        """Send a PUT request through the resilience pipeline."""
        return self.request("PUT", path, **kwargs)

    def delete(self, path: str, **kwargs: Any) -> dict:
        """Send a DELETE request through the resilience pipeline."""
        return self.request("DELETE", path, **kwargs)

    def submit_mutation(
        self,
        method: str,
        path: str,
        *,
        operation: str,
        correlation_id: str | None = None,
        **kwargs: Any,
    ) -> dict:
        """Send a mutating request with uncertain-outcome semantics.

        A timeout/connection loss after send, or a 5xx response, means the
        venue may have accepted the mutation: the correlation key is marked
        unresolved and ``OrderSubmissionUnknownError`` is raised. Resubmission
        under an unresolved key is refused until the caller confirms the
        outcome via an order read and calls ``resolve_uncertain_submission``.
        """
        from tradex_domain import OrderSubmissionUnknownError

        if correlation_id is not None:
            if self._uncertain.is_unresolved(correlation_id):
                raise OrderSubmissionUnknownError(
                    f"order correlation {correlation_id} requires broker "
                    "reconciliation before retry; resolve via get_order first"
                )
            self._uncertain.reserve(correlation_id)
        try:
            result = self.request(method, path, cache_read=False, **kwargs)
        except _UNCERTAIN_SEND_ERRORS as exc:
            if correlation_id is not None:
                self._uncertain.mark_unresolved(correlation_id)
            raise OrderSubmissionUnknownError(
                f"{operation} outcome is unknown (transport failed after send); "
                "reconcile via get_order before retrying"
            ) from exc
        except Exception:
            if correlation_id is not None:
                self._uncertain.commit(correlation_id)
            raise
        # Check for 5xx — server may have accepted the mutation
        if isinstance(result, dict) and result.get("_http_status", 0) >= 500:
            if correlation_id is not None:
                self._uncertain.mark_unresolved(correlation_id)
            raise OrderSubmissionUnknownError(
                f"{operation} got HTTP {result['_http_status']} — outcome uncertain"
            )
        if correlation_id is not None:
            self._uncertain.commit(correlation_id)
        return result

    def resolve_uncertain_submission(self, correlation_id: str) -> None:
        """Clear an unresolved reservation after the outcome was confirmed."""
        self._uncertain.clear(correlation_id)

    def invalidate_cache(self, key: str | None = None) -> int:
        """Drop cached read responses.

        ``None`` clears all; a string is treated as a regex pattern (matching
        cache keys) or a literal key.  Returns the count of evicted entries.
        """
        if self._cache is None:
            return 0
        return self._cache.invalidate(key)

    @property
    def cache_stats(self) -> dict[str, int]:
        """Return cache hit/miss/size counters (zeroed when caching is disabled)."""
        if self._cache is None:
            return {"hits": 0, "misses": 0, "size": 0}
        return {
            "hits": self._cache.hits,
            "misses": self._cache.misses,
            "size": self._cache.size,
        }


__all__ = [
    "AuthRetryPolicy",
    "ProviderHttpClient",
    "UncertainSubmissionTracker",
]
