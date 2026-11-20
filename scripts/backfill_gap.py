#!/usr/bin/env python3
"""Backfill missing data from Oct 23 to Nov 9, 2026."""

import sys
from datetime import datetime
from pathlib import Path

# Add project roots to path
project_root = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(project_root / "market_data" / "src"))
sys.path.insert(0, str(project_root / "brokers" / "src"))
sys.path.insert(0, str(project_root / "domain" / "src"))

from tradex_domain.universe import load_universe
from tradex_market_data.parquet_storage import ParquetStorage
from tradex_market_data.sync_service import SyncConfig, SyncService
from tradex_runtime.live import build_broker_from_env


def main():
    # Build broker and storage
    broker = build_broker_from_env('dhan')
    broker.connect()
    print('✓ Connected to Dhan broker')

    store = ParquetStorage('data')
    print('✓ Storage initialized')

    # Load universe instruments
    instruments = load_universe('nifty500', csv_dir=None)
    print(f'✓ Loaded {len(instruments)} instruments from nifty500 universe')

    # Configure sync service
    config = SyncConfig(
        timeframe='1m',
        batch_size=20,
        max_workers=4,
        budget_seconds=3600,  # 1 hour budget
    )

    sync_service = SyncService(broker, store, config)

    # Run sync for the gap period
    start = datetime(2026, 10, 23)
    end = datetime(2026, 11, 9)

    print('\n=== Starting sync ===')
    print(f'Period: {start.date()} to {end.date()}')
    print(f'Symbols: {len(instruments)}')
    print('=' * 80)

    report = sync_service.sync(instruments, start, end)

    print('\n=== Sync Complete ===')
    print(f'Requested: {report.requested}')
    print(f'Fetched: {report.fetched}')
    print(f'Written: {report.written}')
    print(f'Failed: {len(report.failed)}')
    if report.failed:
        print(f'Failed symbols (first 10): {list(report.failed.keys())[:10]}')
    print(f'Elapsed: {report.elapsed_s:.1f}s')

if __name__ == '__main__':
    main()
