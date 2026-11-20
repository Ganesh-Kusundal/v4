#!/usr/bin/env python3
"""CLI: Unified universe sync manager."""

import argparse
import os
import sys
from pathlib import Path

import pandas as pd

# Add project roots to path
project_root = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(project_root / "market_data" / "src"))
sys.path.insert(0, str(project_root / "brokers" / "src"))
sys.path.insert(0, str(project_root / "domain" / "src"))

from tradex_market_data.parquet_storage import ParquetStorage
from tradex_market_data.universe_sync import UniverseSyncManager


def main():
    parser = argparse.ArgumentParser(
        description="Sync universe, handle renames, retry failures"
    )
    parser.add_argument(
        "--universe",
        default="nifty500",
        help="Universe CSV name (default: nifty500)",
    )
    parser.add_argument(
        "--broker",
        type=str,
        default=None,
        help="Broker provider (dhan/upstox). Reads TRADEDX_BROKER env var if not specified.",
    )
    parser.add_argument(
        "--skip-rename",
        action="store_true",
        help="Skip rename detection",
    )
    parser.add_argument(
        "--skip-retry",
        action="store_true",
        help="Skip failed symbol retry",
    )
    parser.add_argument(
        "--off-peak",
        action="store_true",
        help="Schedule retries for off-peak hours",
    )
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="Show what would change without doing it",
    )
    parser.add_argument(
        "--start-date",
        type=str,
        help="Start date for sync (ISO format, e.g., 2024-01-01)",
    )
    parser.add_argument(
        "--end-date",
        type=str,
        help="End date for sync (ISO format, e.g., 2024-12-31)",
    )

    args = parser.parse_args()

    # Determine broker provider from CLI arg or environment
    import os
    broker_provider = args.broker or os.environ.get("TRADEX_BROKER", "dhan").strip().lower()

    print("Universe Sync Manager CLI")
    print(f"Universe: {args.universe}")
    print(f"Broker: {broker_provider}")
    print(f"Dry run: {args.dry_run}")

    # Build broker and storage
    try:
        from tradex_runtime.live import build_broker_from_env
        broker = build_broker_from_env(broker_provider)
        broker.connect()
        print(f"✓ Connected to {broker_provider} broker")
    except Exception as exc:
        print(f"⚠ Broker connection failed ({exc}) — continuing without broker for dry-run")
        broker = None

    store = ParquetStorage("data")
    print("✓ Storage initialized at data/")

    if args.dry_run:
        print("\nDry run mode — showing planned changes:")
        if broker is None:
            print("  ⚠ No broker available — skipping operations requiring broker")
            return
        manager = UniverseSyncManager(broker=broker, store=store)
        delta = manager.refresh_universe(args.universe)
        print(f"  Would add: {len(delta.added)} symbols")
        print(f"  Would remove: {len(delta.removed)} symbols")
        if not args.skip_rename:
            renames = manager.detect_renames()
            print(f"  Would rename: {len(renames)} symbols")
        return

    print("\nExecuting full sync...")
    if broker is None:
        print("ERROR: Cannot execute sync without broker. Set TRADEDX_BROKER env var or use --broker flag.")
        sys.exit(1)

    manager = UniverseSyncManager(broker=broker, store=store)

    # Parse dates if provided
    start_date = pd.Timestamp(args.start_date) if args.start_date else None
    end_date = pd.Timestamp(args.end_date) if args.end_date else None

    report = manager.sync_all(
        universe_name=args.universe,
        skip_rename=args.skip_rename,
        skip_retry=args.skip_retry,
        start=start_date,
        end=end_date,
    )

    print("\n=== Sync Summary ===")
    print(f"Universe: +{len(report.universe.added)} added, -{len(report.universe.removed)} removed")
    print(f"Renames: {len(report.renames)} migrated")
    if report.retry:
        fetched = len(getattr(report.retry, "fetched_instruments", []))
        print(f"Retry: {fetched} symbols retried")
    print(f"Elapsed: {report.elapsed_s:.0f}s")


if __name__ == "__main__":
    main()
