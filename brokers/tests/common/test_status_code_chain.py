"""Tests for the ``_http_status`` embedding and status-code visibility chain.

Covers transport, retry, circuit breaker, require_success, AuthRetryPolicy,
provider_client submit_mutation, and _FetchResiliencePipeline.
"""

from __future__ import annotations

import json
from unittest.mock import MagicMock, patch

import pytest
from tradex_domain import (
    AuthenticationError,
    BrokerUnavailableError,
    OrderSubmissionUnknownError,
    RateLimitError,
    SDKError,
)

from tradex_brokers.common.circuit_breaker import CircuitBreaker, CircuitState
from tradex_brokers.common.provider_client import (
    AuthRetryPolicy,
    ProviderHttpClient,
)
from tradex_brokers.common.provider_common import require_success
from tradex_brokers.common.retry import RetryableHttpClient
from tradex_brokers.common.transport import HttpTransport

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _mock_urlopen_response(status: int, body: str | dict) -> MagicMock:
    """Build a mock context-manager returned by ``urllib.request.urlopen``."""
    if isinstance(body, dict):
        raw = json.dumps(body).encode()
    else:
        raw = body.encode()
    resp = MagicMock()
    resp.status = status
    resp.read.return_value = raw
    resp.__enter__ = MagicMock(return_value=resp)
    resp.__exit__ = MagicMock(return_value=False)
    return resp


# ===========================================================================
# Transport _http_status embedding
# ===========================================================================


class TestTransportHttpStatus:
    """HttpTransport.request embeds ``_http_status`` in the result dict."""

    @patch("urllib.request.urlopen")
    def test_transport_embeds_http_status_on_success(self, mock_urlopen):
        mock_urlopen.return_value = _mock_urlopen_response(200, {"data": "ok"})
        transport = HttpTransport()
        result = transport.request("GET", "https://example.com/test")
        assert result["_http_status"] == 200

    @patch("urllib.request.urlopen")
    def test_transport_embeds_http_status_for_non_json(self, mock_urlopen):
        mock_urlopen.return_value = _mock_urlopen_response(201, "created")
        transport = HttpTransport()
        result = transport.request("POST", "https://example.com/create")
        assert result == {"data": "created", "_http_status": 201}


# ===========================================================================
# Retry _http_status embedding
# ===========================================================================


class TestRetryHttpStatus:
    """RetryableHttpClient.send embeds ``_http_status`` in the result dict."""

    @patch("urllib.request.urlopen")
    def test_retry_send_embeds_http_status_on_success(self, mock_urlopen):
        mock_urlopen.return_value = _mock_urlopen_response(200, {"data": "ok"})
        client = RetryableHttpClient()
        result = client.send("GET", "https://example.com/test")
        assert result["_http_status"] == 200

    @patch("urllib.request.urlopen")
    def test_retry_send_embeds_http_status_for_non_json(self, mock_urlopen):
        mock_urlopen.return_value = _mock_urlopen_response(200, "raw text")
        client = RetryableHttpClient()
        result = client.send("GET", "https://example.com/test")
        assert result == {"data": "raw text", "_http_status": 200}


# ===========================================================================
# Circuit breaker 5xx detection
# ===========================================================================


class TestCircuitBreaker5xx:
    """Circuit breaker detects 5xx via embedded ``_http_status``."""

    def test_breaker_records_failure_on_5xx(self):
        breaker = CircuitBreaker(failure_threshold=2, recovery_timeout=60.0)
        # First 5xx — still CLOSED
        breaker.request(lambda: {"_http_status": 500})
        assert breaker.state == "CLOSED"
        # Second 5xx — trips OPEN
        breaker.request(lambda: {"_http_status": 500})
        assert breaker.state == "OPEN"

    def test_breaker_records_success_on_2xx(self):
        breaker = CircuitBreaker(failure_threshold=2, recovery_timeout=60.0)
        breaker.request(lambda: {"_http_status": 200})
        assert breaker._failure_count == 0
        assert breaker.state == "CLOSED"

    def test_breaker_5xx_then_success_resets(self):
        breaker = CircuitBreaker(failure_threshold=3, recovery_timeout=60.0)
        breaker.request(lambda: {"_http_status": 502})
        assert breaker._failure_count == 1
        breaker.request(lambda: {"_http_status": 200})
        assert breaker._failure_count == 0

    def test_breaker_non_dict_result_is_success(self):
        breaker = CircuitBreaker(failure_threshold=2, recovery_timeout=60.0)
        result = breaker.request(lambda: "plain_string")
        assert result == "plain_string"
        assert breaker._failure_count == 0

    def test_breaker_dict_without_status_is_success(self):
        breaker = CircuitBreaker(failure_threshold=2, recovery_timeout=60.0)
        result = breaker.request(lambda: {"data": "ok"})
        assert result == {"data": "ok"}
        assert breaker._failure_count == 0

    def test_breaker_half_open_5xx_retrips(self):
        breaker = CircuitBreaker(failure_threshold=1, recovery_timeout=0.01)
        # Trip the breaker
        breaker.request(lambda: {"_http_status": 500})
        assert breaker.state == "OPEN"
        # Manually set HALF_OPEN
        breaker._state = CircuitState.HALF_OPEN
        breaker._opened_at = 0.0
        breaker._success_count = 0
        # 503 in HALF_OPEN → back to OPEN
        breaker.request(lambda: {"_http_status": 503})
        assert breaker.state == "OPEN"


# ===========================================================================
# require_success with _http_status
# ===========================================================================


class TestRequireSuccess:
    """require_success classifies responses using embedded ``_http_status``."""

    def test_require_success_passes_on_200(self):
        result = require_success({"_http_status": 200, "data": "ok"})
        assert result == {"_http_status": 200, "data": "ok"}

    def test_require_success_raises_auth_error_on_401(self):
        with pytest.raises(AuthenticationError):
            require_success({"_http_status": 401})

    def test_require_success_raises_auth_error_on_403(self):
        with pytest.raises(AuthenticationError):
            require_success({"_http_status": 403})

    def test_require_success_raises_rate_limit_on_429(self):
        with pytest.raises(RateLimitError):
            require_success({"_http_status": 429})

    def test_require_success_raises_broker_unavailable_on_500(self):
        with pytest.raises(BrokerUnavailableError):
            require_success({"_http_status": 500})

    def test_require_success_raises_sdk_error_on_4xx(self):
        with pytest.raises(SDKError):
            require_success({"_http_status": 400})

    def test_require_success_raises_sdk_error_on_business_status(self):
        with pytest.raises(SDKError):
            require_success({"status": "error", "message": "failed"})

    def test_require_success_raises_sdk_error_on_error_field(self):
        with pytest.raises(SDKError):
            require_success({"error": {"message": "bad"}})

    def test_require_success_passes_through_clean_dict(self):
        result = require_success({"data": "ok"})
        assert result == {"data": "ok"}

    def test_require_success_raises_on_non_dict(self):
        with pytest.raises(SDKError):
            require_success("not a dict")


# ===========================================================================
# AuthRetryPolicy + provider_client
# ===========================================================================


class TestAuthRetryPolicy:
    """AuthRetryPolicy defaults and provider_client auth retry behavior."""

    def test_auth_retry_policy_defaults(self):
        policy = AuthRetryPolicy()
        assert policy.max_retries == 1
        assert 401 in policy.retryable_statuses
        assert 403 in policy.retryable_statuses

    def test_provider_client_auth_retry_on_401(self):
        mock_pipeline = MagicMock()
        mock_pipeline.send.side_effect = [
            {"_http_status": 401},
            {"_http_status": 200, "data": "ok"},
        ]
        mock_transport = HttpTransport(base_url="https://example.com")
        mock_token_mgr = MagicMock()
        mock_token_mgr.get_token.return_value = "token"
        client = ProviderHttpClient(
            transport=mock_transport,
            pipeline=mock_pipeline,
            token_manager=mock_token_mgr,
            auth_retry_policy=AuthRetryPolicy(),
        )
        client.request("GET", "/test")
        assert mock_pipeline.send.call_count == 2

    def test_provider_client_no_auth_retry_without_policy(self):
        mock_pipeline = MagicMock()
        mock_pipeline.send.return_value = {"_http_status": 401}
        mock_transport = HttpTransport(base_url="https://example.com")
        client = ProviderHttpClient(
            transport=mock_transport,
            pipeline=mock_pipeline,
        )
        client.request("GET", "/test")
        assert mock_pipeline.send.call_count == 1

    def test_submit_mutation_5xx_raises_unknown(self):
        mock_pipeline = MagicMock()
        mock_pipeline.send.return_value = {"_http_status": 502}
        mock_transport = HttpTransport(base_url="https://example.com")
        client = ProviderHttpClient(
            transport=mock_transport,
            pipeline=mock_pipeline,
        )
        with pytest.raises(OrderSubmissionUnknownError):
            client.submit_mutation(
                "POST", "/order", operation="place_order", correlation_id="corr-1"
            )

    def test_submit_mutation_success_commits_key(self):
        mock_pipeline = MagicMock()
        mock_pipeline.send.return_value = {"_http_status": 200}
        mock_transport = HttpTransport(base_url="https://example.com")
        client = ProviderHttpClient(
            transport=mock_transport,
            pipeline=mock_pipeline,
        )
        result = client.submit_mutation(
            "POST", "/order", operation="place_order", correlation_id="corr-2"
        )
        assert result["_http_status"] == 200
        # Key should NOT be unresolved after success
        assert not client._uncertain.is_unresolved("corr-2")

    def test_submit_mutation_unresolved_refuses_retry(self):
        mock_pipeline = MagicMock()
        mock_pipeline.send.return_value = {"_http_status": 200}
        mock_transport = HttpTransport(base_url="https://example.com")
        client = ProviderHttpClient(
            transport=mock_transport,
            pipeline=mock_pipeline,
        )
        # Mark key unresolved
        client._uncertain.mark_unresolved("corr-3")
        with pytest.raises(OrderSubmissionUnknownError):
            client.submit_mutation(
                "POST", "/order", operation="place_order", correlation_id="corr-3"
            )
        # Pipeline should NOT have been called
        assert mock_pipeline.send.call_count == 0


# ===========================================================================
# _FetchResiliencePipeline
# ===========================================================================


class TestFetchResiliencePipeline:
    """_FetchResiliencePipeline wraps fetch callables and embeds status."""

    def test_fetch_pipeline_embeds_status_from_tuple(self):
        from tradex_brokers.common.client_shared import FetchResiliencePipeline

        fetch = MagicMock(return_value=(200, {"data": "ok"}))
        pipeline = FetchResiliencePipeline(fetch)
        result = pipeline.send("GET", "https://example.com/test")
        assert result["_http_status"] == 200
        assert result["data"] == "ok"

    def test_fetch_pipeline_wraps_non_dict_body(self):
        from tradex_brokers.common.client_shared import FetchResiliencePipeline

        fetch = MagicMock(return_value=(201, "created"))
        pipeline = FetchResiliencePipeline(fetch)
        result = pipeline.send("POST", "https://example.com/create")
        assert result == {"data": "created", "_http_status": 201}

    def test_fetch_pipeline_passes_through_dict(self):
        from tradex_brokers.common.client_shared import FetchResiliencePipeline

        fetch = MagicMock(return_value={"data": "already_dict"})
        pipeline = FetchResiliencePipeline(fetch)
        result = pipeline.send("GET", "https://example.com/test")
        assert result == {"data": "already_dict"}
