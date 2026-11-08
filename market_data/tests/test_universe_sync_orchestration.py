from datetime import datetime, timezone
from unittest.mock import MagicMock, patch


def test_sync_all_pipeline(tmp_path):
    from tradex_market_data.universe_sync import (
        UniverseSyncManager,
        UniverseDelta,
        MigrationResult,
    )

    broker = MagicMock()
    store = MagicMock()

    manager = UniverseSyncManager(broker=broker, store=store, datalake_root=tmp_path)

    # Mock methods
    with (
        patch.object(manager, "refresh_universe") as mock_refresh,
        patch.object(manager, "detect_renames") as mock_renames,
        patch.object(manager, "migrate_symbol_lake") as mock_migrate,
        patch.object(manager, "retry_failed") as mock_retry,
    ):
        mock_refresh.return_value = UniverseDelta(
            added=[],
            removed=[],
            unchanged=[],
            last_refreshed=datetime.now(timezone.utc),
        )
        mock_renames.return_value = []
        mock_migrate.return_value = MigrationResult(success=True)
        mock_retry.return_value = None

        report = manager.sync_all("nifty500")

        assert report.universe is not None
        assert mock_refresh.called
        assert mock_renames.called
        assert report.elapsed_s > 0
