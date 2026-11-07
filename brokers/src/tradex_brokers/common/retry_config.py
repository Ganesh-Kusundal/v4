"""Retry and backoff configuration for WebSocket and HTTP operations.

Centralizes retry/backoff parameters to avoid scattered values across
WebSocket reconnection and HTTP retry logic.
"""

from __future__ import annotations

import random
import urllib.error
from dataclasses import dataclass
from typing import Any


@dataclass
class WSRetryConfig:
    """WebSocket reconnection retry configuration."""

    max_retries: int = 10
    base_delay: float = 5.0
    max_delay: float = 300.0
    exponential_base: float = 2.0
    jitter: bool = True

    def delay_for_attempt(self, attempt: int) -> float:
        delay = min(
            self.base_delay * (self.exponential_base ** attempt),
            self.max_delay,
        )
        if self.jitter:
            delay = delay * (0.5 + random.random())  # noqa: S311
        return delay


@dataclass
class HTTPRetryConfig:
    """HTTP-specific retry configuration (more aggressive than WebSocket)."""

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
        delay = min(
            self.base_delay * (self.exponential_base ** attempt),
            self.max_delay,
        )
        if self.jitter:
            delay = delay * (0.5 + random.random())  # noqa: S311
        return delay


# Backward-compatible aliases for the consolidated single-class names.
RetryConfig = HTTPRetryConfig

# Singleton instances for easy import
WS_RETRY_CONFIG = WSRetryConfig()
HTTP_RETRY_CONFIG = HTTPRetryConfig()
