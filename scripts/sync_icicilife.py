#!/usr/bin/env python3
"""Sync ICICILIFE to fill the Oct 23 - Nov 6 gap."""

import sys
from datetime import datetime
from pathlib import Path

# Add project roots to path
project_root = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(project_root / "market_data" / "src"))
sys.path.insert(0, str(project_root / "brokers" / "src"))
sys.path.insert(0, str(project_root / "domain" / "src"))

from tradex_domain.instruments import Equity
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

    # Create instrument for ICICILIFE
    instrument = Equity.of('NSE', 'ICICILIFE')
    print(f'✓ Instrument: {instrument.symbol}')

    # Configure sync service
    config = SyncConfig(
        timeframe='1m',
        batch_size=20,
        max_workers=2,
        budget_seconds=300,  # 5 minutes should be enough for 1 symbol
    )

    sync_service = SyncService(broker, store, config)

    # Run sync for the gap period
    start = datetime(2026, 10, 23)
    end = datetime(2026, 11, 9)

    print('\n=== Starting sync for ICICILIFE ===')
    print(f'Period: {start.date()} to {end.date()}')
    print('=' * 80)

    report = sync_service.sync([instrument], start, end)

    print('\n=== Sync Complete ===')
    print(f'Requested: {report.requested}')
    print(f'Fetched: {report.fetched}')
    print(f'Written: {report.written}')
    print(f'Failed: {len(report.failed)}')
    if report.failed:
        print(f'Failed symbols: {list(report.failed.keys())}')
    print(f'Elapsed: {report.elapsed_s:.1f}s')

if __name__ == '__main__':
    main()
