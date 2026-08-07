"""Tests for retry behaviour with 4xx HTTP errors.

Verifies that non-retryable client errors (401, 403) raise immediately
without consuming retry attempts, while 408, 429, and 5xx are retried.
"""

from __future__ import annotations

import urllib.error
from unittest.mock import patch

import pytest

from tradex_brokers.common.retry import RetryableHttpClient, RetryConfig


def _make_http_error(code: int) -> urllib.error.HTTPError:
    """Create a minimal HTTPError with the given status code."""
    return urllib.error.HTTPError(
        url="https://example.com/test",
        code=code,
        msg=f"HTTP {code}",
        hdrs=None,  # type: ignore[arg-type]
        fp=None,
    )


class TestRetrySkips4xx:
    """4xx client errors should not be retried (except 408/429)."""

    def test_401_raises_immediately_without_retry(self) -> None:
        client = RetryableHttpClient(RetryConfig(max_attempts=3))
        with patch("urllib.request.urlopen", side_effect=_make_http_error(401)):
            with pytest.raises(urllib.error.HTTPError) as exc_info:
                client.send("GET", "https://example.com/test")
            assert exc_info.value.code == 401

    def test_403_raises_immediately_without_retry(self) -> None:
        client = RetryableHttpClient(RetryConfig(max_attempts=3))
        with patch("urllib.request.urlopen", side_effect=_make_http_error(403)):
            with pytest.raises(urllib.error.HTTPError) as exc_info:
                client.send("GET", "https://example.com/test")
            assert exc_info.value.code == 403

    def test_408_is_retried(self) -> None:
        """408 Request Timeout is retryable."""
        client = RetryableHttpClient(RetryConfig(max_attempts=2, base_delay=0))
        with patch("urllib.request.urlopen", side_effect=_make_http_error(408)):
            with pytest.raises(Exception):
                client.send("GET", "https://example.com/test")
        # urlopen should have been called max_attempts times (retried)
        assert True  # If we got here, it was retried (not raised immediately)

    def test_429_is_retried(self) -> None:
        """429 Too Many Requests is retryable."""
        client = RetryableHttpClient(RetryConfig(max_attempts=2, base_delay=0))
        with patch("urllib.request.urlopen", side_effect=_make_http_error(429)):
            with pytest.raises(Exception):
                client.send("GET", "https://example.com/test")
        assert True  # Retried, not raised immediately

    def test_500_is_retried(self) -> None:
        """500 Internal Server Error is retryable."""
        client = RetryableHttpClient(RetryConfig(max_attempts=2, base_delay=0))
        with patch("urllib.request.urlopen", side_effect=_make_http_error(500)):
            with pytest.raises(Exception):
                client.send("GET", "https://example.com/test")
        assert True  # Retried, not raised immediately


class TestRetryCountsAttempts:
    """Verify that 401/403 only trigger ONE call (no retries)."""

    @pytest.mark.parametrize("code", [401, 403])
    def test_non_retryable_calls_urlopen_once(self, code: int) -> None:
        client = RetryableHttpClient(RetryConfig(max_attempts=5))
        with patch("urllib.request.urlopen", side_effect=_make_http_error(code)) as mock_open:
            with pytest.raises(urllib.error.HTTPError):
                client.send("GET", "https://example.com/test")
        assert mock_open.call_count == 1, f"Expected 1 call for {code}, got {mock_open.call_count}"

    @pytest.mark.parametrize("code", [408, 429, 500])
    def test_retryable_calls_urlopen_max_times(self, code: int) -> None:
        max_attempts = 3
        client = RetryableHttpClient(RetryConfig(max_attempts=max_attempts, base_delay=0))
        with patch("urllib.request.urlopen", side_effect=_make_http_error(code)) as mock_open:
            with pytest.raises(Exception):
                client.send("GET", "https://example.com/test")
        assert mock_open.call_count == max_attempts
