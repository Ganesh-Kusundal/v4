"""API-key auth and consumer admission control."""

from __future__ import annotations

import hmac
import threading
import time
from dataclasses import dataclass, field

from tradex_gateway.config import GatewayConfig
from tradex_gateway.errors import Unauthorized


def check_api_key(provided: str | None, config: GatewayConfig) -> None:
    if config.api_key is None:
        if config.allow_anon:
            return
        raise Unauthorized("gateway API key is not configured")
    if provided is None or not hmac.compare_digest(str(provided), config.api_key):
        raise Unauthorized("invalid API key")


@dataclass
class _Bucket:
    tokens: float
    last: float


@dataclass
class TokenBucket:
    """Per-key token bucket; refills continuously at *rate* per second."""

    rate: float
    burst: int
    clock: object = time.monotonic
    _buckets: dict[str, _Bucket] = field(default_factory=dict)
    _lock: threading.Lock = field(default_factory=threading.Lock)

    def consume(self, key: str, tokens: float = 1.0) -> tuple[bool, float]:
        now = float(self.clock())
        bucket = self._buckets.get(key)
        if bucket is None:
            bucket = _Bucket(tokens=float(self.burst), last=now)
            self._buckets[key] = bucket
        elapsed = max(0.0, now - bucket.last)
        bucket.tokens = min(float(self.burst), bucket.tokens + elapsed * self.rate)
        bucket.last = now
        if bucket.tokens >= tokens:
            bucket.tokens -= tokens
            return True, 0.0
        missing = tokens - bucket.tokens
        return False, missing / self.rate if self.rate > 0 else 1.0


def identity_for(api_key: str | None, host: str | None) -> str:
    return f"key:{api_key}" if api_key else f"ip:{host or 'unknown'}"
