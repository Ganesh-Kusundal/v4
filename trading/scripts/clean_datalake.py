#!/usr/bin/env python3
"""Clean datalake: strip phantom post-market bars, keep NSE market hours only (9:15-15:30 IST).

Usage:
    python trading/scripts/clean_datalake.py [--data-root data/]
"""

from __future__ import annotations

import argparse
import time
from pathlib import Path

import pandas as pd
import pyarrow as pa
import pyarrow.parquet as pq


def clean_parquet_file(path: Path) -> tuple[int, int]:
    """Filter a single parquet file to market hours. Returns (before, after) row counts."""
    df = pd.read_parquet(path)
    before = len(df)
    if df.empty or "timestamp" not in df.columns:
        return before, before

    df["timestamp"] = pd.to_datetime(df["timestamp"])

    # NSE market hours: 9:15 - 15:30
    mask = (
        ((df["timestamp"].dt.hour == 9) & (df["timestamp"].dt.minute >= 15))
        | ((df["timestamp"].dt.hour >= 10) & (df["timestamp"].dt.hour < 15))
        | ((df["timestamp"].dt.hour == 15) & (df["timestamp"].dt.minute <= 30))
    )
    cleaned = df[mask]
    after = len(cleaned)

    if after < before:
        table = pa.Table.from_pandas(cleaned, preserve_index=False)
        pq.write_table(table, str(path), use_dictionary=False)

    return before, after


def main() -> int:
    p = argparse.ArgumentParser(description="Clean datalake: strip phantom post-market bars")
    p.add_argument("--data-root", default="data/", help="Parquet base path (default: data/)")
    p.add_argument("--dry-run", action="store_true", help="Count only, don't rewrite")
    args = p.parse_args()

    root = Path(args.data_root) / "ohlcv"
    if not root.exists():
        print(f"ERROR: {root} not found")
        return 1

    parquet_files = list(root.rglob("data.parquet"))
    print(f"Found {len(parquet_files)} parquet files")

    total_before = 0
    total_after = 0
    t0 = time.monotonic()

    for i, pf in enumerate(parquet_files):
        before, after = clean_parquet_file(pf) if not args.dry_run else (
            len(pd.read_parquet(pf)), len(pd.read_parquet(pf))  # dry run: just count
        )
        total_before += before
        total_after += after

        if (i + 1) % 200 == 0:
            elapsed = time.monotonic() - t0
            removed = total_before - total_after
            print(f"  [{i + 1}/{len(parquet_files)}] {elapsed:.1f}s  removed={removed:,}")

    removed = total_before - total_after
    elapsed = time.monotonic() - t0

    print(f"\n{'=' * 60}")
    print(f"CLEANUP {'(DRY RUN)' if args.dry_run else 'COMPLETE'}")
    print(f"{'=' * 60}")
    print(f"Files processed:  {len(parquet_files)}")
    print(f"Bars before:      {total_before:,}")
    print(f"Bars after:       {total_after:,}")
    print(f"Phantom removed:  {removed:,} ({removed / total_before * 100:.1f}%)")
    print(f"Time:             {elapsed:.1f}s")

    if not args.dry_run:
        new_size = sum(pf.stat().st_size for pf in parquet_files)
        print(f"New total size:   {new_size / 1024 / 1024:.0f} MB")

    return 0


if __name__ == "__main__":
    raise SystemExit(main())
