"""Task 2: ``build_provider_client`` wires the composed ``ResiliencePipeline``.

The fetch-injection seam must survive: the pipeline's retry client routes onto
the injected ``fetch`` via its transport slot, so tests never touch the network.
"""

from __future__ import annotations

from typing import Any

from tradex_brokers.common.client_shared import build_provider_client
from tradex_brokers.common.resilience import (
    CircuitBreaker,
    ResiliencePipeline,
    RetryableHttpClient,
)


def _fetch(method: str, url: str, **kwargs: Any):
    return 200, {"ok": True}


def test_build_provider_client_wires_resilience_pipeline() -> None:
    http, _ws = build_provider_client(
        fetch=_fetch, base_url="https://api.dhan.co/v2",
        auth_headers=lambda t: {"access-token": t}, provider="dhan",
    )
    assert isinstance(http._pipeline, ResiliencePipeline)


def test_pipeline_retry_client_uses_injected_fetch_as_transport() -> None:
    http, _ws = build_provider_client(
        fetch=_fetch, base_url="https://api.dhan.co/v2",
        auth_headers=lambda t: {"access-token": t}, provider="dhan",
    )
    retry = http._pipeline._retry
    assert isinstance(retry, RetryableHttpClient)
    assert retry._transport is not None
    assert retry._transport("GET", "https://api.dhan.co/v2/orders") == (200, {"ok": True})


def test_pipeline_composed_of_limiter_retry_breaker() -> None:
    http, _ws = build_provider_client(
        fetch=_fetch, base_url="https://api.dhan.co/v2",
        auth_headers=lambda t: {"access-token": t}, provider="dhan",
    )
    assert isinstance(http._pipeline._breaker, CircuitBreaker)


def test_send_through_wired_pipeline_preserves_fetch_shape() -> None:
    """The seam tests' ``(status, dict-body)`` tuple must come through intact."""
    http, _ws = build_provider_client(
        fetch=_fetch, base_url="https://api.dhan.co/v2",
        auth_headers=lambda t: {"access-token": t}, provider="dhan",
    )
    result = http._pipeline.send("GET", "https://api.dhan.co/v2/orders")
    assert result["ok"] is True
    assert result["_http_status"] == 200
