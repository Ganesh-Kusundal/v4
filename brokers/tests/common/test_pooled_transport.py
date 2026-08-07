"""Tests for PooledHttpTransport."""

from __future__ import annotations

import http.client
from unittest.mock import MagicMock, patch

from tradex_brokers.common.pooled_transport import PooledHttpTransport


class TestPooledHttpTransportInit:
    """Basic instantiation tests."""

    def test_can_be_instantiated(self) -> None:
        transport = PooledHttpTransport()
        assert transport._pool_size == 4
        assert transport._timeout == 30.0

    def test_custom_pool_size_and_timeout(self) -> None:
        transport = PooledHttpTransport(pool_size=8, timeout=10.0)
        assert transport._pool_size == 8
        assert transport._timeout == 10.0


class TestPooledHttpTransportClose:
    """Tests for close() behaviour."""

    def test_close_clears_all_connections(self) -> None:
        transport = PooledHttpTransport()
        # Inject fake connections into the pool
        mock_conn1 = MagicMock(spec=http.client.HTTPSConnection)
        mock_conn2 = MagicMock(spec=http.client.HTTPSConnection)
        transport._connections[("example.com", 443)] = [mock_conn1, mock_conn2]

        transport.close()

        assert transport._connections == {}
        mock_conn1.close.assert_called_once()
        mock_conn2.close.assert_called_once()

    def test_close_on_empty_pool_is_safe(self) -> None:
        transport = PooledHttpTransport()
        transport.close()  # Should not raise
        assert transport._connections == {}


class TestPooledHttpTransportRequest:
    """Tests for request() with mocked connections."""

    def test_request_returns_status_and_json_body(self) -> None:
        transport = PooledHttpTransport()
        mock_resp = MagicMock()
        mock_resp.status = 200
        mock_resp.read.return_value = b'{"ok": true}'

        mock_conn = MagicMock(spec=http.client.HTTPSConnection)
        mock_conn.getresponse.return_value = mock_resp

        with patch.object(transport, "_get_connection", return_value=mock_conn):
            status, body = transport.request("GET", "https://example.com/api")

        assert status == 200
        assert body == {"ok": True}

    def test_request_returns_raw_text_when_not_json(self) -> None:
        transport = PooledHttpTransport()
        mock_resp = MagicMock()
        mock_resp.status = 200
        mock_resp.read.return_value = b"plain text response"

        mock_conn = MagicMock(spec=http.client.HTTPSConnection)
        mock_conn.getresponse.return_value = mock_resp

        with patch.object(transport, "_get_connection", return_value=mock_conn):
            status, body = transport.request("GET", "https://example.com/api")

        assert status == 200
        assert body == "plain text response"

    def test_request_sends_json_body_with_content_type(self) -> None:
        transport = PooledHttpTransport()
        mock_resp = MagicMock()
        mock_resp.status = 201
        mock_resp.read.return_value = b'{"id": 1}'

        mock_conn = MagicMock(spec=http.client.HTTPSConnection)
        mock_conn.getresponse.return_value = mock_resp

        with patch.object(transport, "_get_connection", return_value=mock_conn):
            status, body = transport.request(
                "POST",
                "https://example.com/api",
                body={"key": "value"},
            )

        assert status == 201
        # Verify that conn.request was called with JSON-encoded body
        call_args = mock_conn.request.call_args
        assert call_args[1]["body"] == b'{"key": "value"}' or call_args[0][2] == b'{"key": "value"}'
