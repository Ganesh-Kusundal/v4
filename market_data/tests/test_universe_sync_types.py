from datetime import UTC, datetime, timedelta, timezone

from tradex_market_data.universe_sync import (
    FailureRecord,
    MigrationResult,
    RenameCandidate,
    UniverseDelta,
)


def test_universe_delta_creation():
    delta = UniverseDelta(
        added=[],
        removed=["OLDSTOCK"],
        unchanged=[],
        last_refreshed=datetime.now(UTC),
    )
    assert len(delta.removed) == 1
    assert delta.last_refreshed.tzinfo is not None

def test_rename_candidate_defaults():
    candidate = RenameCandidate(
        old_symbol="HEG",
        new_symbol="HEGAM",
        isin="INE545A01024",
    )
    assert candidate.confidence == 1.0
    assert candidate.status == "pending"

def test_failure_record_retry_after():
    record = FailureRecord(
        symbol="ABLBL",
        failed_at=datetime.now(UTC),
        reason="rate_limited",
        attempts=1,
        last_error="Rate limit exceeded",
        retry_after=datetime.now(UTC) + timedelta(hours=1),
    )
    assert record.retry_after is not None
    assert record.attempts == 1
