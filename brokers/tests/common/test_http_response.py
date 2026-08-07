"""Tests for HttpResponse dataclass and require_success_response."""

from __future__ import annotations

import pytest
from tradex_domain.errors import (
    AuthenticationError,
    BrokerUnavailableError,
    RateLimitError,
    SDKError,
)

from tradex_brokers.common.http_response import HttpResponse, require_success_response


class TestHttpResponseIsSuccess:
    """Test the is_success property for various HTTP status codes."""

    def test_200_is_success(self) -> None:
        """200 OK should be a success."""
        response = HttpResponse(status=200, body={"data": "ok"})
        assert response.is_success is True

    def test_404_is_not_success(self) -> None:
        """404 Not Found should not be a success."""
        response = HttpResponse(status=404, body={"error": "not found"})
        assert response.is_success is False

    def test_500_is_not_success(self) -> None:
        """500 Internal Server Error should not be a success."""
        response = HttpResponse(status=500, body={"error": "server error"})
        assert response.is_success is False


class TestRequireSuccessResponse:
    """Test require_success_response error classification."""

    def test_success_response_returned_unchanged(self) -> None:
        """Successful responses should be returned unchanged."""
        response = HttpResponse(status=200, body={"data": "ok"})
        result = require_success_response(response, "test_operation")
        assert result is response

    def test_401_raises_authentication_error(self) -> None:
        """401 Unauthorized should raise AuthenticationError."""
        response = HttpResponse(status=401, body={"error": "unauthorized"})
        with pytest.raises(AuthenticationError, match="Authentication failed.*401"):
            require_success_response(response, "test_operation")

    def test_429_raises_rate_limit_error(self) -> None:
        """429 Too Many Requests should raise RateLimitError."""
        response = HttpResponse(status=429, body={"error": "rate limited"})
        with pytest.raises(RateLimitError, match="Rate limit exceeded.*429"):
            require_success_response(response, "test_operation")

    def test_400_raises_sdk_error(self) -> None:
        """400 Bad Request should raise SDKError (client error)."""
        response = HttpResponse(status=400, body={"error": "bad request"})
        with pytest.raises(SDKError, match="Client error.*400"):
            require_success_response(response, "test_operation")

    def test_500_raises_broker_unavailable_error(self) -> None:
        """500 Internal Server Error should raise BrokerUnavailableError."""
        response = HttpResponse(status=500, body={"error": "server error"})
        with pytest.raises(BrokerUnavailableError, match="Server error.*500"):
            require_success_response(response, "test_operation")
