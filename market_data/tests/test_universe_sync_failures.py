from datetime import datetime, timedelta, timezone
from tradex_market_data.universe_sync import FailureRecord, UniverseSyncManager


def test_failure_persistence_round_trip(tmp_path):
    manager = UniverseSyncManager(broker=None, store=None, datalake_root=tmp_path)

    # Save failures
    records = {
        "ABLBL": FailureRecord(
            symbol="ABLBL",
            failed_at=datetime.now(timezone.utc),
            reason="rate_limited",
            attempts=1,
            last_error="Rate limit exceeded",
            retry_after=datetime.now(timezone.utc) + timedelta(hours=1),
        ),
    }
    manager.save_failures(records)

    # Load failures
    loaded = manager.load_failures()

    assert "ABLBL" in loaded
    assert loaded["ABLBL"].reason == "rate_limited"
    assert loaded["ABLBL"].attempts == 1
