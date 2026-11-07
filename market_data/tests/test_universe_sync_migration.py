"""Tests for atomic lake migration in universe sync."""

import pandas as pd
from datetime import datetime
from tradex_market_data.universe_sync import UniverseSyncManager


def test_migration_atomic_rollback(tmp_path):
    # Setup: create old symbol directory with parquet file
    datalake = tmp_path / "data"
    ohlcv = datalake / "ohlcv" / "symbol=HEG" / "year=2024" / "month=01"
    ohlcv.mkdir(parents=True)

    # Create test parquet
    df = pd.DataFrame([{"timestamp": datetime(2024, 1, 2, 9, 15), "symbol": "HEG", "open": 100}])
    df.to_parquet(ohlcv / "data.parquet", index=False)

    manager = UniverseSyncManager(broker=None, store=None, datalake_root=datalake)

    result = manager.migrate_symbol_lake("HEG", "HEGAM", "INE545A01024")

    assert result.success
    assert result.rows_migrated > 0
    assert (datalake / "ohlcv" / "symbol=HEGAM").exists()
    assert not (datalake / "ohlcv" / "symbol=HEG").exists()

    # Verify parquet symbol column updated
    migrated_file = datalake / "ohlcv" / "symbol=HEGAM" / "year=2024" / "month=01" / "data.parquet"
    df_migrated = pd.read_parquet(migrated_file)
    assert df_migrated["symbol"].iloc[0] == "HEGAM"
