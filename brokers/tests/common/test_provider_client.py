"""Ported from v3 ``test_provider_api_clients.py`` — ProviderHttpClient basics.

v4 ``ProviderHttpClient`` composes ``HttpTransport`` + a ``send``-pipeline
+ optional ``DurableTokenManager``.  The constructor is completely different
from v3.  These tests exercise the composition pattern and auth injection.
"""

from __future__ import annotations

from support.fetch_pipeline import FetchResiliencePipeline
from tradex_brokers.common.provider_client import ProviderHttpClient, UncertainSubmissionTracker
from tradex_brokers.common.token_lifecycle import PortTokenManager
from tradex_brokers.common.transport import HttpTransport


class _FakePort:
    """Minimal TokenLifecyclePort for testing."""

    def __init__(self, token: str = "test-token") -> None:
        self._token = token
        self._expired = False

    def get_access_token(self) -> str:
        return self._token

    def refresh(self) -> str:
        return self._token

    def is_expired(self) -> bool:
        return self._expired


def _make_pipeline() -> FetchResiliencePipeline:
    """Build a permissive fetch-routed pipeline for testing."""
    return FetchResiliencePipeline(lambda _method, _url, **kwargs: {"data": {}})


# ---------------------------------------------------------------------------
# Construction + auth injection
# ---------------------------------------------------------------------------


def test_provider_client_construction() -> None:
    transport = HttpTransport(base_url="https://api.example.com")
    pipeline = _make_pipeline()
    client = ProviderHttpClient(transport=transport, pipeline=pipeline)
    assert client._transport is transport
    assert client._pipeline is pipeline
    assert client._token_manager is None


def test_provider_client_with_token_manager() -> None:
    transport = HttpTransport(base_url="https://api.example.com")
    pipeline = _make_pipeline()
    port = _FakePort(token="my-token")
    token_mgr = PortTokenManager(port=port)
    client = ProviderHttpClient(
        transport=transport, pipeline=pipeline, token_manager=token_mgr,
    )
    assert client._token_manager is token_mgr


def test_provider_client_auth_injection() -> None:
    transport = HttpTransport(base_url="https://api.example.com")
    port = _FakePort(token="my-token")
    token_mgr = PortTokenManager(port=port)
    client = ProviderHttpClient(
        transport=transport, pipeline=_make_pipeline(), token_manager=token_mgr,
    )
    kwargs, token_sent = client._inject_auth({})
    assert kwargs["headers"]["Authorization"] == "Bearer my-token"
    assert token_sent == "my-token"


def test_provider_client_auth_skipped_without_manager() -> None:
    transport = HttpTransport(base_url="https://api.example.com")
    client = ProviderHttpClient(transport=transport, pipeline=_make_pipeline())
    kwargs, token_sent = client._inject_auth({})
    assert "Authorization" not in kwargs.get("headers", {})
    assert token_sent is None


def test_provider_client_build_url_from_base() -> None:
    transport = HttpTransport(base_url="https://api.example.com")
    client = ProviderHttpClient(transport=transport, pipeline=_make_pipeline())
    assert client._build_url("/orders") == "https://api.example.com/orders"


def test_provider_client_build_url_absolute_passthrough() -> None:
    transport = HttpTransport(base_url="https://api.example.com")
    client = ProviderHttpClient(transport=transport, pipeline=_make_pipeline())
    assert client._build_url("https://other.com/x") == "https://other.com/x"


def test_provider_client_build_url_no_base() -> None:
    transport = HttpTransport()
    client = ProviderHttpClient(transport=transport, pipeline=_make_pipeline())
    assert client._build_url("/orders") == "/orders"


# ---------------------------------------------------------------------------
# UncertainSubmissionTracker
# ---------------------------------------------------------------------------


def test_uncertain_tracker_reserve_and_commit() -> None:
    tracker = UncertainSubmissionTracker()
    tracker.reserve("key-1")
    assert not tracker.is_unresolved("key-1")
    tracker.commit("key-1")


def test_uncertain_tracker_mark_unresolved() -> None:
    tracker = UncertainSubmissionTracker()
    tracker.reserve("key-2")
    tracker.mark_unresolved("key-2")
    assert tracker.is_unresolved("key-2")


def test_uncertain_tracker_clear() -> None:
    tracker = UncertainSubmissionTracker()
    tracker.reserve("key-3")
    tracker.mark_unresolved("key-3")
    tracker.clear("key-3")
    assert not tracker.is_unresolved("key-3")


# ---------------------------------------------------------------------------
# ProviderHttpClient — caching
# ---------------------------------------------------------------------------


def test_provider_client_cache_stats_disabled() -> None:
    transport = HttpTransport(base_url="https://api.example.com")
    client = ProviderHttpClient(transport=transport, pipeline=_make_pipeline())
    stats = client.cache_stats
    assert stats == {"hits": 0, "misses": 0, "size": 0}


def test_provider_client_cache_stats_enabled() -> None:
    transport = HttpTransport(base_url="https://api.example.com")
    client = ProviderHttpClient(
        transport=transport,
        pipeline=_make_pipeline(),
        cache_ttl_seconds=60.0,
    )
    stats = client.cache_stats
    assert stats["size"] == 0


def test_provider_client_invalidate_cache_noop_when_disabled() -> None:
    transport = HttpTransport(base_url="https://api.example.com")
    client = ProviderHttpClient(transport=transport, pipeline=_make_pipeline())
    client.invalidate_cache()  # should not raise


def test_provider_client_invalidate_cache_when_enabled() -> None:
    transport = HttpTransport(base_url="https://api.example.com")
    client = ProviderHttpClient(
        transport=transport,
        pipeline=_make_pipeline(),
        cache_ttl_seconds=60.0,
    )
    client.invalidate_cache()  # should not raise


# ---------------------------------------------------------------------------
# ProviderHttpClient — request() method
# ---------------------------------------------------------------------------


def test_provider_client_request_delegates_to_pipeline() -> None:
    """request() builds URL, injects auth, and calls the pipeline."""
    transport = HttpTransport(base_url="https://api.example.com")
    client = ProviderHttpClient(transport=transport, pipeline=_make_pipeline())
    # _should_cache returns False when caching is disabled
    assert client._should_cache("GET", None) is False
    assert client._should_cache("GET", False) is False


def test_provider_client_should_cache_safe_methods() -> None:
    transport = HttpTransport(base_url="https://api.example.com")
    client = ProviderHttpClient(
        transport=transport,
        pipeline=_make_pipeline(),
        cache_ttl_seconds=30.0,
    )
    assert client._should_cache("GET", None) is True
    assert client._should_cache("HEAD", None) is True
    assert client._should_cache("POST", None) is False
    assert client._should_cache("POST", True) is True
    assert client._should_cache("GET", False) is False


# ---------------------------------------------------------------------------
# ProviderHttpClient — resolve_uncertain_submission
# ---------------------------------------------------------------------------


def test_provider_client_resolve_uncertain_submission() -> None:
    transport = HttpTransport(base_url="https://api.example.com")
    client = ProviderHttpClient(transport=transport, pipeline=_make_pipeline())
    client._uncertain.reserve("corr-1")
    client._uncertain.mark_unresolved("corr-1")
    assert client._uncertain.is_unresolved("corr-1")
    client.resolve_uncertain_submission("corr-1")
    assert not client._uncertain.is_unresolved("corr-1")
