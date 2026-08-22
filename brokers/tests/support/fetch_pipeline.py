"""Test-support pipeline adapter for ProviderHttpClient unit tests.

``FetchResiliencePipeline`` routes through an injected *fetch* callable with
no rate limiting, retry, or circuit breaking, so tests can drive the
``ProviderHttpClient`` status/auth/cache logic deterministically. It is a
test seam only — production binds ``common.resilience.ResiliencePipeline``
(see ``build_provider_client``).
"""

from __future__ import annotations

from collections.abc import Callable
from typing import Any

__all__ = ["FetchResiliencePipeline"]


class FetchResiliencePipeline:
    """Pipeline adapter that routes through an injected *fetch* callable."""

    def __init__(self, fetch: Callable[..., Any]) -> None:
        self._fetch = fetch

    def send(self, method: str, url: str, **kwargs: Any) -> Any:
        result = self._fetch(method, url, **kwargs)
        if isinstance(result, tuple) and len(result) == 2:
            _status, body = result
            # Carry the HTTP status so auth-retry classification can
            # distinguish 401/403 outright rejections from business bodies.
            if isinstance(body, dict):
                body["_http_status"] = _status
                return body
            return {"data": body, "_http_status": _status}
        if isinstance(result, dict):
            return result
        return {"data": result}