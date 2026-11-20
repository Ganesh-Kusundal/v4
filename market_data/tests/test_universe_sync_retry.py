from datetime import UTC, datetime, timedelta, timezone
from unittest.mock import MagicMock

from tradex_market_data.universe_sync import FailureRecord, UniverseSyncManager


def test_retry_classification_rate_limited():
    manager = UniverseSyncManager(broker=None, store=None)

    reason = manager.classify_failure("Rate limiter could not provide token within 30.0s")
    assert reason == "rate_limited"

    reason = manager.classify_failure("HTTP 429 Too Many Requests")
    assert reason == "rate_limited"


def test_retry_backoff_doubling():
    manager = UniverseSyncManager(broker=None, store=None)
    now = datetime.now(UTC)

    record = FailureRecord(
        symbol="TEST",
        failed_at=now,
        reason="rate_limited",
        attempts=2,
        last_error="Rate limited",
    )

    next_retry = manager._calculate_next_retry(record)
    expected_delay = 30 * (2 ** (record.attempts - 1))  # 30s * 2^1 = 60s
    expected_retry = now + timedelta(seconds=expected_delay)

    assert abs((next_retry - expected_retry).total_seconds()) < 1
