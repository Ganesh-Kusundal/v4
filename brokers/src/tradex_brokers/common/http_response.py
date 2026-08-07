"""Typed HTTP response wrapper with v3-parity error classification.

Provides a dataclass for HTTP responses and a helper function that
maps HTTP status codes to the appropriate domain exceptions, matching
the error classification behavior from v3.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any

from tradex_domain.errors import (
    AuthenticationError,
    BrokerUnavailableError,
    RateLimitError,
    SDKError,
)


@dataclass
class HttpResponse:
    """Typed HTTP response with status code and body."""

    status: int
    body: Any

    @property
    def is_success(self) -> bool:
        """Return True if the status code indicates success (2xx)."""
        return 200 <= self.status < 300


def require_success_response(response: HttpResponse, operation: str) -> HttpResponse:
    """v3-parity status-based error classification.

    Raises the appropriate domain exception for non-success HTTP responses:
    - 401/403 → AuthenticationError
    - 429 → RateLimitError
    - 4xx (other) → SDKError (client error)
    - 5xx → BrokerUnavailableError (server error)

    Returns the response unchanged if it indicates success.
    """
    if response.is_success:
        return response
    if response.status in (401, 403):
        raise AuthenticationError(f"Authentication failed (HTTP {response.status})")
    if response.status == 429:
        raise RateLimitError(f"Rate limit exceeded (HTTP {response.status})")
    if 400 <= response.status < 500:
        raise SDKError(f"Client error (HTTP {response.status})")
    if response.status >= 500:
        raise BrokerUnavailableError(f"Server error (HTTP {response.status})")
    return response


__all__ = ["HttpResponse", "require_success_response"]
