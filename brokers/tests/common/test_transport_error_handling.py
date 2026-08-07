"""Tests for HttpTransport error wrapping, URL building, and header merging."""

from __future__ import annotations

import urllib.error
import urllib.request
from unittest.mock import MagicMock, patch

import pytest
from tradex_domain import BrokerUnavailableError

from tradex_brokers.common.transport import HttpTransport

# ---------------------------------------------------------------------------
# Error wrapping
# ---------------------------------------------------------------------------


class TestTransportErrorHandling:
    """HttpTransport wraps all transport errors into BrokerUnavailableError."""

    def test_http_error_wraps_to_broker_unavailable_with_body(self) -> None:
        """HTTPError → BrokerUnavailableError with the error body in the message."""
        transport = HttpTransport(base_url="https://api.example.com")
        err = urllib.error.HTTPError(
            url="https://api.example.com/orders",
            code=500,
            msg="Internal Server Error",
            hdrs=MagicMock(),
            fp=None,
        )
        # Provide a readable body
        err.read = MagicMock(return_value=b'{"error": "boom"}')

        with patch.object(urllib.request, "urlopen", side_effect=err):
            with pytest.raises(BrokerUnavailableError, match="HTTP 500"):
                transport.request("POST", "/orders")

    def test_url_error_wraps_to_broker_unavailable(self) -> None:
        """URLError → BrokerUnavailableError."""
        transport = HttpTransport(base_url="https://api.example.com")
        err = urllib.error.URLError(reason="connection refused")

        with patch.object(urllib.request, "urlopen", side_effect=err):
            with pytest.raises(BrokerUnavailableError, match="URL error"):
                transport.request("GET", "/quotes")

    def test_connection_timeout_os_error_wraps_to_broker_unavailable(self) -> None:
        """TimeoutError → ConnectionTimeoutError (subclass of BrokerUnavailableError)."""
        transport = HttpTransport(base_url="https://api.example.com")

        # TimeoutError is distinguished as ConnectionTimeoutError (subclass of
        # BrokerUnavailableError) so callers can tell timeout from broker-down.
        with patch.object(urllib.request, "urlopen", side_effect=TimeoutError("fail")):
            with pytest.raises(BrokerUnavailableError, match="timed out"):
                transport.request("GET", "/health")

        # ConnectionError / OSError still wrap as plain BrokerUnavailableError
        for exc_cls in (ConnectionError, OSError):
            with patch.object(urllib.request, "urlopen", side_effect=exc_cls("fail")):
                with pytest.raises(BrokerUnavailableError, match="Transport error"):
                    transport.request("GET", "/health")

    # ---------------------------------------------------------------------------
    # URL building
    # ---------------------------------------------------------------------------

    def test_build_url_with_params_encodes_query(self) -> None:
        """URL building with query params encodes them into the URL."""
        transport = HttpTransport(base_url="https://api.example.com")
        url = transport._build_url("/orders", params={"symbol": "AAPL", "limit": 10})
        assert url.startswith("https://api.example.com/orders?")
        assert "symbol=AAPL" in url
        assert "limit=10" in url

    # ---------------------------------------------------------------------------
    # Header merging
    # ---------------------------------------------------------------------------

    def test_merge_headers_extra_overrides_defaults(self) -> None:
        """Extra headers override default headers."""
        transport = HttpTransport(
            base_url="https://api.example.com",
            default_headers={"Authorization": "Bearer old-token", "Accept": "application/json"},
        )
        merged = transport._merge_headers({"Authorization": "Bearer new-token"})
        assert merged["Authorization"] == "Bearer new-token"
        assert merged["Accept"] == "application/json"
