#!/usr/bin/env python3
"""Rename a symbol's hive partition in the datalake (corporate rename).

Usage::

    python trading/scripts/rename_symbol.py --from HEG --to HEGAM
"""

from __future__ import annotations

import argparse
import shutil
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent.parent
for sub in ("domain/src", "brokers/src", "trading/src"):
    sys.path.insert(0, str(ROOT / sub))

import pandas as pd  # noqa: E402

from tradex_trading.datalake.paths import datalake_root  # noqa: E402


def main(argv: list[str] | None = None) -> int:
    p = argparse.ArgumentParser(description="Rename datalake symbol partition")
    p.add_argument("--from", dest="src", required=True)
    p.add_argument("--to", dest="dst", required=True)
    p.add_argument("--data-root", default=None)
    p.add_argument("--dry-run", action="store_true")
    args = p.parse_args(argv)

    src, dst = args.src.strip().upper(), args.dst.strip().upper()
    if src == dst:
        print("from == to — nothing to do")
        return 0

    root = Path(args.data_root) if args.data_root else Path(datalake_root())
    ohlcv = root / "ohlcv" if (root / "ohlcv").exists() else root
    src_dir = ohlcv / f"symbol={src}"
    dst_dir = ohlcv / f"symbol={dst}"
    if not src_dir.exists():
        print(f"missing {src_dir}")
        return 1
    if dst_dir.exists():
        print(f"target exists {dst_dir} — refuse to merge; move or delete first")
        return 1

    files = list(src_dir.rglob("*.parquet"))
    print(f"{src} → {dst}: {len(files)} parquet files under {src_dir}")
    if args.dry_run:
        return 0

    for fp in files:
        rel = fp.relative_to(src_dir)
        out = dst_dir / rel
        out.parent.mkdir(parents=True, exist_ok=True)
        df = pd.read_parquet(fp)
        if "symbol" in df.columns:
            df["symbol"] = dst
        df.to_parquet(out, index=False)
    shutil.rmtree(src_dir)
    print(f"done — removed {src_dir}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
