"""Retry with exponential backoff for broker HTTP calls.

Provides a dataclass configuration object and a lightweight HTTP client that
retries transient failures using only the standard library.
"""

from __future__ import annotations

import json
import logging
import random
import time
import urllib.error
import urllib.parse
import urllib.request
from dataclasses import dataclass
from typing import Any

from tradex_domain import BrokerUnavailableError

log = logging.getLogger(__name__)

_SAFE_METHODS = frozenset({"GET", "HEAD", "OPTIONS"})


class RetryExhaustedError(BrokerUnavailableError):
    """Raised when every retry attempt returned a retryable status.

    Subclasses :class:`~tradex_domain.errors.BrokerUnavailableError` so callers
    that catch ``BrokerUnavailableError`` also catch retry exhaustion — the
    transport layer raises ``BrokerUnavailableError`` on permanent failures,
    so ``RetryExhaustedError`` must follow the same hierarchy for
    zero-parity between backtest/replay/live.

    Attributes
    ----------
    last_status:
        HTTP status of the final attempt (``None`` when exhaustion came from
        a raised transport error).
    """

    def __init__(self, message: str, *, last_status: int | None = None) -> None:
        super().__init__(message)
        self.last_status = last_status


def retryable(method: str) -> bool:
    """Return ``True`` for idempotent HTTP methods that are safe to auto-retry.

    Reads (GET, HEAD, OPTIONS) are safe to retry automatically; writes
    (POST, PUT, DELETE) are not because they may have side-effects.
    """
    return method.strip().upper() in _SAFE_METHODS




@dataclass
class RetryConfig:
    """Configuration for retry behaviour."""

    max_attempts: int = 3
    base_delay: float = 0.1
    max_delay: float = 30.0
    exponential_base: float = 2.0
    jitter: bool = True
    retryable_exceptions: tuple[type[Exception], ...] = (
        ConnectionError,
        TimeoutError,
        urllib.error.URLError,
        urllib.error.HTTPError,
    )

    def delay_for_attempt(self, attempt: int) -> float:
        """Compute the delay before the next retry attempt (0-indexed)."""
        delay = min(
            self.base_delay * (self.exponential_base ** attempt),
            self.max_delay,
        )
        if self.jitter:
            delay = delay * (0.5 + random.random())  # noqa: S311
        return delay


class RetryableHttpClient:
    """HTTP client with automatic retry on transient failures.

    Uses only ``urllib`` from the standard library — no external HTTP deps.
    """

    def __init__(self, config: RetryConfig | None = None) -> None:
        self._config = config or RetryConfig()

    @property
    def config(self) -> RetryConfig:
        return self._config

    @staticmethod
    def is_safe_method(method: str) -> bool:
        """Return ``True`` for idempotent HTTP methods (GET/HEAD/OPTIONS)."""
        return method.upper() in _SAFE_METHODS

    # -- public API ---------------------------------------------------------

    def send(self, method: str, url: str, **kwargs: Any) -> Any:
        """Send an HTTP request with retries on transient failures.

        Parameters
        ----------
        method:
            HTTP method (GET, POST, PUT, DELETE, …).
        url:
            Fully-qualified URL.
        **kwargs:
            Optional *headers* (dict), *json* (serialisable body),
            *params* (query-string dict), *timeout* (float).

        Returns
        -------
        dict
            Parsed JSON response body, or raw text wrapped in
            ``{"data": <text>}`` when the response is not JSON.

        Raises
        ------
        RetryExhaustedError
            After all retry attempts are exhausted (subclass of
            ``BrokerUnavailableError`` for zero-parity with the transport layer).
        """
        headers: dict[str, str] = kwargs.get("headers") or {}
        body: bytes | None = None
        json_payload = kwargs.get("json")
        if json_payload is not None:
            body = json.dumps(json_payload).encode()
            headers.setdefault("Content-Type", "application/json")

        params = kwargs.get("params")
        if params:
            qs = urllib.parse.urlencode(params)
            sep = "&" if "?" in url else "?"
            url = f"{url}{sep}{qs}"

        timeout: float = kwargs.get("timeout", 30.0)
        last_exc: Exception | None = None

        for attempt in range(self._config.max_attempts):
            try:
                req = urllib.request.Request(
                    url,
                    data=body,
                    headers=headers,
                    method=method.upper(),
                )
                with urllib.request.urlopen(req, timeout=timeout) as resp:
                    raw = resp.read().decode()
                    try:
                        result = json.loads(raw)
                    except (json.JSONDecodeError, ValueError):
                        result = {"data": raw}
                    result["_http_status"] = resp.status
                    return result
            except tuple(self._config.retryable_exceptions) as exc:
                # Non-retryable 4xx client errors (except 408 Timeout, 429 Rate Limit)
                if isinstance(exc, urllib.error.HTTPError) and 400 <= exc.code < 500:
                    if exc.code not in (408, 429):
                        raise  # e.g. 401 Unauthorized, 403 Forbidden
                last_exc = exc
                if attempt < self._config.max_attempts - 1:
                    delay = self._config.delay_for_attempt(attempt)
                    log.warning(
                        "Request to %s failed (attempt %d/%d): %s — retrying in %.2fs",
                        url,
                        attempt + 1,
                        self._config.max_attempts,
                        exc,
                        delay,
                    )
                    time.sleep(delay)
                else:
                    log.error(
                        "Request to %s failed after %d attempts: %s",
                        url,
                        self._config.max_attempts,
                        exc,
                    )

        raise RetryExhaustedError(
            f"Request to {url} failed after {self._config.max_attempts} attempts",
            last_status=(
                last_exc.code
                if isinstance(last_exc, urllib.error.HTTPError)
                else None
            ),
        ) from last_exc


__all__ = [
    "RetryConfig",
    "RetryExhaustedError",
    "RetryableHttpClient",
    "retryable",
]
