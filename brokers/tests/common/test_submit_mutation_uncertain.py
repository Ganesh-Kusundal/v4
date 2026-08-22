"""Tests for ProviderHttpClient submit_mutation uncertain semantics and auth retry."""

from __future__ import annotations

from unittest.mock import MagicMock, patch

import pytest
from tradex_domain import OrderSubmissionUnknownError

from support.fetch_pipeline import FetchResiliencePipeline
from tradex_brokers.common.provider_client import (
    AuthRetryPolicy,
    ProviderHttpClient,
)
from tradex_brokers.common.transport import HttpTransport

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _make_pipeline() -> FetchResiliencePipeline:
    return FetchResiliencePipeline(lambda _method, _url, **kwargs: {"data": {}})


def _make_client(**kwargs) -> ProviderHttpClient:
    transport = MagicMock(spec=HttpTransport)
    transport._base_url = "https://api.example.com"
    pipeline = _make_pipeline()
    return ProviderHttpClient(transport=transport, pipeline=pipeline, **kwargs)


# ---------------------------------------------------------------------------
# submit_mutation uncertain semantics
# ---------------------------------------------------------------------------


class TestSubmitMutationUncertain:
    """Tests for uncertain mutation submission semantics."""

    def test_submit_mutation_oserror_marks_unresolved(self) -> None:
        """OSError during submit_mutation → OrderSubmissionUnknownError + unresolved."""
        client = _make_client()
        # Make request() raise OSError (which is in _UNCERTAIN_SEND_ERRORS)
        with patch.object(client, "request", side_effect=OSError("connection lost")):
            with pytest.raises(OrderSubmissionUnknownError, match="outcome is unknown"):
                client.submit_mutation(
                    "POST", "/orders",
                    operation="place_order",
                    correlation_id="corr-123",
                )
        # Key should be marked unresolved
        assert client._uncertain.is_unresolved("corr-123")

    def test_submit_mutation_non_os_exception_commits_key(self) -> None:
        """Non-OSError exception during submit_mutation → key freed (committed)."""
        client = _make_client()
        with patch.object(client, "request", side_effect=ValueError("bad input")):
            with pytest.raises(ValueError, match="bad input"):
                client.submit_mutation(
                    "POST", "/orders",
                    operation="place_order",
                    correlation_id="corr-456",
                )
        # Key should NOT be unresolved (it was committed/freed)
        assert not client._uncertain.is_unresolved("corr-456")

    def test_submit_mutation_no_correlation_id_skips_tracking(self) -> None:
        """correlation_id=None skips uncertain tracking entirely."""
        client = _make_client()
        with patch.object(client, "request", side_effect=OSError("connection lost")):
            with pytest.raises(OrderSubmissionUnknownError):
                client.submit_mutation(
                    "POST", "/orders",
                    operation="place_order",
                    correlation_id=None,
                )
        # No key was reserved or marked unresolved
        assert client._uncertain.is_unresolved("anything") is False

    # ---------------------------------------------------------------------------
    # Auth retry
    # ---------------------------------------------------------------------------

    def test_auth_retry_on_403(self) -> None:
        """403 response triggers auth retry (re-fetches token and retries)."""
        token_mgr = MagicMock()
        token_mgr.get_token.return_value = "initial-token"

        client = _make_client(
            token_manager=token_mgr,
            auth_retry_policy=AuthRetryPolicy(max_retries=1),
        )
        # First call returns 403, second returns success
        call_count = 0
        def fake_send(method, url, **kwargs):
            nonlocal call_count
            call_count += 1
            if call_count == 1:
                return {"_http_status": 403, "error": "forbidden"}
            return {"_http_status": 200, "data": "ok"}

        with patch.object(client._pipeline, "send", side_effect=fake_send):
            result = client.request("GET", "/orders")

        assert result["_http_status"] == 200
        assert call_count == 2

    def test_auth_retry_on_business_token_rejection(self) -> None:
        """Business-level token rejection in response body triggers auth retry."""
        token_mgr = MagicMock()
        token_mgr.get_token.return_value = "initial-token"

        client = _make_client(
            token_manager=token_mgr,
            auth_retry_policy=AuthRetryPolicy(max_retries=1),
        )
        call_count = 0
        def fake_send(method, url, **kwargs):
            nonlocal call_count
            call_count += 1
            if call_count == 1:
                # Business-level token rejection (no _http_status set)
                # Uses "invalid_token" which is in _TOKEN_REJECTION_MARKERS
                return {
                    "status": "error",
                    "error_code": "invalid_token",
                    "message": "session expired",
                }
            return {"status": "success", "data": "ok"}

        with patch.object(client._pipeline, "send", side_effect=fake_send):
            result = client.request("GET", "/profile")

        assert result["status"] == "success"
        assert call_count == 2

    # ---------------------------------------------------------------------------
    # Cache key
    # ---------------------------------------------------------------------------

    def test_cache_key_deterministic_excludes_headers(self) -> None:
        """Cache key is the same regardless of headers (auth tokens excluded)."""
        key1 = ProviderHttpClient._cache_key(
            "GET", "https://api.example.com/orders",
            {"headers": {"Authorization": "Bearer token-A"}, "params": {"limit": 10}},
        )
        key2 = ProviderHttpClient._cache_key(
            "GET", "https://api.example.com/orders",
            {"headers": {"Authorization": "Bearer token-B"}, "params": {"limit": 10}},
        )
        assert key1 == key2
        # But different params produce different keys
        key3 = ProviderHttpClient._cache_key(
            "GET", "https://api.example.com/orders",
            {"headers": {}, "params": {"limit": 20}},
        )
        assert key1 != key3
