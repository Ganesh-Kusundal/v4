"""Shared plumbing for broker REST clients (Dhan, Upstox).

Broker-independent pieces of the per-provider API clients:

- :func:`parse_timestamp_fallback` — wrap :func:`parse_timestamp` with a
  fallback for empty values.
- :func:`correlation_id` — parse a native correlation id with a deterministic
  uuid5 fallback seed.
- :class:`~tradex_brokers.common.resilience.ResiliencePipeline` — the composed
  rate-limit → circuit-breaker → safe-retry pipeline ``build_provider_client``
  binds into :class:`~tradex_brokers.common.provider_client.ProviderHttpClient`;
  its retry client's transport is the injected ``fetch``, so tests and offline
  tools never touch the network.
"""

from __future__ import annotations

from collections.abc import Callable
from datetime import datetime
from typing import Any
from uuid import NAMESPACE_URL, UUID, uuid5

from tradex_domain.errors import SDKError
from tradex_domain.execution import OrderResult
from tradex_domain.value_objects import CorrelationId, OrderId

from tradex_brokers.common.provider_client import ProviderHttpClient
from tradex_brokers.common.provider_common import parse_timestamp
from tradex_brokers.common.resilience import (
    CircuitBreaker,
    ResiliencePipeline,
    RetryableHttpClient,
    limiter_for_provider,
)
from tradex_brokers.common.token_lifecycle import TokenLifecyclePort
from tradex_brokers.common.transport import HttpTransport


def parse_timestamp_fallback(value: object, fallback: datetime) -> datetime:
    """Parse a timestamp, returning *fallback* for None/empty."""
    if value is None or value == "":
        return fallback
    try:
        # ponytail: pass value directly — str() breaks epoch int/float parsing
        return parse_timestamp(value)  # type: ignore[arg-type]
    except (SDKError, TypeError):
        try:
            return parse_timestamp(str(value))
        except SDKError:
            return fallback


def correlation_id(raw: object, *, fallback_seed: str) -> CorrelationId:
    """Parse the native correlationId; deterministic uuid5 fallback for empty.

    A broker echoes back exactly the id we sent (e.g. the strategy bridge's
    non-UUID ``strat-...`` ids), so a non-empty value is preserved verbatim —
    hashing it would silently break order matching in the live-fill bridge.
    Only missing/empty values fall back to a deterministic uuid5 of the seed.
    """
    text = str(raw or "")
    if not text:
        return CorrelationId(value=uuid5(NAMESPACE_URL, fallback_seed))
    try:
        return CorrelationId(value=UUID(text))
    except (ValueError, AttributeError):
        return CorrelationId(value=text)


def build_provider_client(
    *,
    fetch: Callable[..., Any],
    base_url: str,
    auth_headers: Callable[[str], dict[str, str]],
    token_manager: TokenLifecyclePort | None = None,
    access_token: str = "",
    provider: str,
) -> tuple[ProviderHttpClient, Callable[[], str] | None]:
    """Compose a ``ProviderHttpClient`` around an injected *fetch*.

    Shared by ``DhanApiClient.from_fetch`` and ``UpstoxApiClient.from_fetch``:
    the two clients differ only in their ``auth_headers`` mapping (Dhan uses
    ``access-token``/``client-id``, Upstox a ``Bearer`` header).

    The request pipeline is a :class:`ResiliencePipeline` (rate-limit →
    circuit-breaker → safe retry).  The injected *fetch* becomes the retry
    client's transport, so the same fetch-injection seam that tests rely on
    also carries every production HTTP call — nothing touches ``urllib``.
    The retry client inspects the returned ``_http_status``, so a 429/5xx
    response on a safe method (GET/HEAD/OPTIONS) is retried before it reaches
    the downstream status chain, and a 429 additionally puts its rate-limit
    bucket into cooldown.

    Returns ``(http, ws_token_provider)`` — the provider-specific clients keep
    their own base/host defaults and registry wiring.
    """
    if (provider or "").strip().lower() not in ("dhan", "upstox", "paper"):
        raise ValueError(f"unknown provider: {provider!r}")
    # Adapter: funnels the injected fetch into the retry client's transport
    # slot.  ``RetryableHttpClient.send`` normalises the result to the
    # ``(status, body)`` contract, preserving the legacy FetchResiliencePipeline
    # shapes the seam tests exercise.
    def _fetch_transport(method: str, url: str, **kwargs: Any) -> Any:
        return fetch(method, url, **kwargs)

    pipeline = ResiliencePipeline(
        rate_limiter=limiter_for_provider(provider),
        retry=RetryableHttpClient(transport=_fetch_transport),
        breaker=CircuitBreaker(),
    )
    on_auth_failure: Callable[[str], None] | None
    if token_manager is not None:
        token_provider = token_manager.ensure_token

        def on_auth_failure(rejected: str) -> None:
            token_manager.ensure_token(rejected_token=rejected)  # type: ignore[attr-defined]

    else:
        static_token = access_token or ""

        def token_provider() -> str:  # type: ignore[misc]
            return static_token

        on_auth_failure = None

    transport = HttpTransport(
        base_url=base_url,
        token_provider=token_provider,  # type: ignore[arg-type]
        auth_headers=auth_headers,
        on_auth_failure=on_auth_failure)
    http = ProviderHttpClient(
        transport=transport,
        pipeline=pipeline,  # type: ignore[arg-type]
    )
    if token_manager is not None:
        def ws_token_provider() -> str:  # type: ignore[misc]
            return token_manager.ensure_token()

    else:
        def ws_token_provider() -> str:  # type: ignore[misc]
            return access_token

    return http, ws_token_provider


def order_result_from_dict(
    raw: object,
    *,
    fallback_id: OrderId | None = None,
) -> OrderResult:
    """Build a frozen :class:`OrderResult` from a provider dict.

    Tolerates the ``order_id``/``orderId`` and ``status``/``message`` key
    variants returned by the Dhan and Upstox REST surfaces.
    """
    if not isinstance(raw, dict):
        raw = {}
    order_id_raw = raw.get("order_id", raw.get("orderId"))
    if order_id_raw:
        order_id = OrderId(value=str(order_id_raw))
    elif fallback_id is not None:
        order_id = fallback_id
    else:
        order_id = OrderId(value="")
    status = str(raw.get("status", ""))
    message = str(raw.get("message", ""))
    return OrderResult(order_id=order_id, status=status, message=message)


__all__ = [
    "build_provider_client",
    "correlation_id",
    "order_result_from_dict",
    "parse_timestamp_fallback",
]
