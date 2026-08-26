"""Smoke test for ProviderHttpClient.rate_limiter exposure."""
from unittest.mock import MagicMock

from tradex_brokers.common.provider_client import ProviderHttpClient
from tradex_brokers.common.resilience import MultiBucketRateLimiter, RateLimitConfig


def test_provider_http_client_exposes_rate_limiter():
    """ProviderHttpClient.rate_limiter is the same instance the pipeline holds."""
    limiter = MultiBucketRateLimiter(default=RateLimitConfig())
    pipeline = MagicMock()
    pipeline._rate_limiter = limiter
    client = ProviderHttpClient(transport=MagicMock(), pipeline=pipeline)

    assert client.rate_limiter is limiter
