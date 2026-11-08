#!/usr/bin/env python3
"""CLI: Unified universe sync manager."""

import argparse
import sys
from pathlib import Path

# Add project roots to path
project_root = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(project_root / "market_data" / "src"))
sys.path.insert(0, str(project_root / "brokers" / "src"))
sys.path.insert(0, str(project_root / "domain" / "src"))

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

    # TODO: Wire up actual broker and store once integration is ready
    # For now, show placeholder
    print("Universe Sync Manager CLI")
    print(f"Universe: {args.universe}")
    print(f"Dry run: {args.dry_run}")

    if args.dry_run:
        print("\nDry run mode — showing planned changes:")
        print(f"  Would refresh universe: {args.universe}")
        if not args.skip_rename:
            print("  Would detect renames")
        if not args.skip_retry:
            print("  Would retry failed symbols")
        return

    print("\nExecuting full sync...")
    # manager = UniverseSyncManager(broker=..., store=...)
    # report = manager.sync_all(...)
    print("TODO: Integrate with broker and store")


if __name__ == "__main__":
    main()
