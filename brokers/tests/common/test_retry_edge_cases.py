"""Tests for retry edge cases: transient retries, exhaustion, and backoff jitter."""

from __future__ import annotations

import urllib.request
from unittest.mock import patch

import pytest
from tradex_domain import BrokerUnavailableError

from tradex_brokers.common.retry import RetryableHttpClient, RetryConfig

# ---------------------------------------------------------------------------
# Tests
# ---------------------------------------------------------------------------


class TestRetryEdgeCases:
    """Retry behaviour for transient failures and exhaustion."""

    def test_retry_connection_error_is_retried(self) -> None:
        """ConnectionError triggers retry; success on later attempt returns result."""
        config = RetryConfig(max_attempts=3, base_delay=0.0, jitter=False)
        client = RetryableHttpClient(config)

        call_count = 0

        def fake_urlopen(req, timeout=None):
            nonlocal call_count
            call_count += 1
            if call_count < 3:
                raise ConnectionError("connection refused")
            # Third call succeeds
            import io
            resp = io.BytesIO(b'{"ok": true}')
            resp.status = 200
            resp.read = lambda: b'{"ok": true}'
            resp.__enter__ = lambda s: s
            resp.__exit__ = lambda s, *a: None
            return resp

        with patch.object(urllib.request, "urlopen", side_effect=fake_urlopen):
            result = client.send("GET", "https://api.example.com/health")

        assert result["ok"] is True
        assert call_count == 3

    def test_retry_exhaustion_raises_broker_unavailable(self) -> None:
        """After max attempts exhausted, raises BrokerUnavailableError."""
        config = RetryConfig(max_attempts=2, base_delay=0.0, jitter=False)
        client = RetryableHttpClient(config)

        with patch.object(
            urllib.request, "urlopen",
            side_effect=ConnectionError("always fails"),
        ):
            with pytest.raises(BrokerUnavailableError, match="failed after 2 attempts"):
                client.send("GET", "https://api.example.com/health")

    def test_retry_delay_for_attempt_with_jitter(self) -> None:
        """delay_for_attempt with jitter returns value within expected range."""
        config = RetryConfig(
            max_attempts=5,
            base_delay=1.0,
            max_delay=30.0,
            exponential_base=2.0,
            jitter=True,
        )
        # attempt=0: base delay = 1.0 * 2^0 = 1.0; with jitter: [0.5, 1.5]
        for _ in range(20):
            delay = config.delay_for_attempt(0)
            assert 0.5 <= delay <= 1.5, f"delay {delay} out of range [0.5, 1.5]"

        # attempt=3: base delay = 1.0 * 2^3 = 8.0; with jitter: [4.0, 12.0]
        for _ in range(20):
            delay = config.delay_for_attempt(3)
            assert 4.0 <= delay <= 12.0, f"delay {delay} out of range [4.0, 12.0]"

        # Without jitter
        config_no_jitter = RetryConfig(
            base_delay=1.0, exponential_base=2.0, jitter=False,
        )
        assert config_no_jitter.delay_for_attempt(0) == 1.0
        assert config_no_jitter.delay_for_attempt(2) == 4.0
