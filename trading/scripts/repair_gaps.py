#!/usr/bin/env python3
"""Resumable, priority-ordered gap repair driver (one bounded slice per run).

Why not just `fill_gaps.py --tail-days 0 --min-gap-stamps 2`:

- fill_gaps processes clusters biggest-first, which would put the 500-symbol
  missing 2026-09-15 session ahead of the 210-symbol 2026-08-31 mass gap;
- a whole-job foreground run outlives the shell that started it and risks being
  killed mid-write, so this slices the work: each invocation runs for at most
  ``--budget`` seconds, exits cleanly between chunks, and re-detects on the next
  run. Every batch upserts as it completes, so progress is durable and the next
  slice picks up whatever is still missing.

Ordering, worst-value-last:

1. **interior holes** — real bars missing mid-session, widest first (2026-08-31:
   210 symbols × the 15:15–15:28 tail);
2. **missing whole sessions** — a day with nothing at all (2026-09-15);
3. **session-open stamps** — the intermittent 09:15 bar, one per symbol-day.

Usage::

    python trading/scripts/repair_gaps.py --budget 480
    python trading/scripts/repair_gaps.py --budget 480 --no-open-stamps
    python trading/scripts/repair_gaps.py --only-symbols CHOLAFIN --days 90
"""

from __future__ import annotations

import argparse
import logging
import sys
import time
from collections import defaultdict
from datetime import datetime, timedelta
from itertools import batched
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent.parent
for sub in ("domain/src", "brokers/src", "trading/src"):
    sys.path.insert(0, str(ROOT / sub))

from tradex_domain.market_calendar import (  # noqa: E402
    MARKET_CLOSE,
    MARKET_OPEN,
    NSE_HOLIDAYS_2026,
)

from tradex_trading.config.env import load_env_file  # noqa: E402

load_env_file(str(ROOT / ".env.local"))

from tradex_trading.datalake.gap_detector import GapDetector  # noqa: E402
from tradex_trading.datalake.parquet_storage import ParquetStorage  # noqa: E402
from tradex_trading.datalake.simple_sync import simple_sync  # noqa: E402
from tradex_trading.datalake.universe import load_universe  # noqa: E402
from tradex_trading.runtime.live import build_broker_from_env  # noqa: E402

logging.basicConfig(level=logging.WARNING,
                    format="%(asctime)s  %(levelname)-7s  %(name)s  %(message)s")
log = logging.getLogger("repair-gaps")

#: A range this wide is a whole session, not a hole inside one.
_WHOLE_SESSION_STAMPS = 370


def _kind(gs: datetime, ge: datetime) -> str:
    """Classify one missing range: interior hole, whole session, or open stamp."""
    if (ge - gs) >= timedelta(minutes=_WHOLE_SESSION_STAMPS):
        return "session"
    if gs == ge and gs.time() == MARKET_OPEN:
        return "open"
    return "interior"


def _clusters(found) -> dict[tuple, dict]:
    """Group missing ranges into (first day, last day) clusters with a kind.

    Keyed per *range*, not per symbol: a symbol can be missing the open on one
    day and a tail on another, and keying off its widest span would fuse those
    days into one cluster spanning a month.

    A cluster takes the most substantial kind any of its ranges has — a day is
    a whole-session job if any symbol is missing all of it, an interior job if
    anyone is missing real mid-session bars, and an open-stamp repair only when
    every range in it is a bare 09:15 stamp.
    """
    groups: dict[tuple, dict] = defaultdict(lambda: {"kinds": set(), "symbols": set()})
    for inst, ranges in found.gaps:
        for gs, ge in ranges:
            key = (gs.date(), ge.date())
            groups[key]["symbols"].add(inst.symbol)
            groups[key]["kinds"].add(_kind(gs, ge))
    out: dict[tuple, dict] = {}
    for key, bucket in groups.items():
        kinds = bucket["kinds"]
        kind = ("session" if "session" in kinds
                else "interior" if "interior" in kinds else "open")
        out[key] = {"kind": kind, "symbols": bucket["symbols"]}
    return out


_ORDER = {"interior": 0, "session": 1, "open": 2}


def main(argv: list[str] | None = None) -> int:
    p = argparse.ArgumentParser(description="Priority-ordered resumable gap repair")
    p.add_argument("--days", type=int, default=90,
                   help="Scan window length in days (default: 90)")
    p.add_argument("--universe", default="nifty500")
    p.add_argument("--timeframe", default="1m")
    p.add_argument("--min-gap-stamps", type=int, default=2)
    p.add_argument("--no-open-stamps", action="store_true",
                   help="Skip the 09:15 session-open repair")
    p.add_argument("--skip-symbols", default="",
                   help="Comma-separated symbols to leave out — for tickers no "
                        "configured broker can serve, which otherwise burn a "
                        "retry cycle per cluster. Check the resolution before "
                        "reaching for this: a symbol that returns an empty "
                        "series is usually resolving to the wrong security "
                        "(two rows sharing a trading symbol), not unservable")
    p.add_argument("--only-symbols", default="",
                   help="Comma-separated symbols to restrict the repair to — "
                        "for proving a resolution or fetch fix on one symbol "
                        "instead of running the whole universe")
    p.add_argument("--budget", type=float, default=480.0,
                   help="Seconds to work before exiting cleanly (default: 480)")
    p.add_argument("--chunk", type=int, default=80,
                   help="Symbols per sync call, so a huge cluster still exits "
                        "cleanly at the budget (default: 80)")
    p.add_argument("--data-root", default=None)
    args = p.parse_args(argv)

    started = time.monotonic()
    store = ParquetStorage(Path(args.data_root) if args.data_root else ROOT / "data")
    detector = GapDetector(store)
    skipped = {s.strip().upper() for s in args.skip_symbols.split(",") if s.strip()}
    only = {s.strip().upper() for s in args.only_symbols.split(",") if s.strip()}
    universe = load_universe(args.universe)
    known = {i.symbol for i in universe}
    if only - known:
        print(f"[repair] not in {args.universe}: {sorted(only - known)}", flush=True)
    instruments = [
        i for i in universe
        if i.symbol not in skipped and (not only or i.symbol in only)
    ]
    if not instruments:
        print("[repair] no instruments selected", flush=True)
        return 1
    by_symbol = {i.symbol: i for i in instruments}
    if skipped:
        print(f"[repair] skipping {sorted(skipped)}", flush=True)
    if only:
        print(f"[repair] only {sorted(only)}", flush=True)

    now = datetime.now().replace(second=0, microsecond=0)
    start = (now - timedelta(days=args.days)).replace(
        hour=0, minute=0, second=0, microsecond=0)
    end = min(datetime.combine((now - timedelta(days=1)).date(), MARKET_CLOSE), now)
    open_stamps = not args.no_open_stamps

    def scan():
        return detector.scan(
            instruments, start=start, end=end, timeframe=args.timeframe,
            bar_freq="1min", holidays=NSE_HOLIDAYS_2026,
            min_gap_stamps=args.min_gap_stamps, max_workers=8, tail_days=None,
            include_open_stamps=open_stamps,
        )

    print(f"[repair] window {start.date()} -> {end:%Y-%m-%d %H:%M}", flush=True)
    found = scan()
    groups = _clusters(found)
    print(f"[repair] before: {found.gapped_symbols} symbols gapped in "
          f"{len(groups)} day-clusters | edge-only={len(found.edge_only)} "
          f"open_missing={found.open_missing_symbols} "
          f"close_missing={found.close_missing_symbols}", flush=True)
    if not groups:
        print("[repair] nothing to repair", flush=True)
        return 0

    order = sorted(
        groups.items(),
        key=lambda kv: (_ORDER[kv[1]["kind"]], -len(kv[1]["symbols"])),
    )

    try:
        dhan = build_broker_from_env("dhan")
        dhan.connect()
    except Exception as exc:
        print(f"[repair] dhan unavailable: {exc}", flush=True)
        return 1

    gap_map = {inst.symbol: r for inst, r in found.gaps}
    written = done = 0
    stop = False
    for (first, last), bucket in order:
        symbols = sorted(bucket["symbols"])
        insts = [by_symbol[s] for s in symbols if s in by_symbol]
        ws = datetime.combine(first, datetime.min.time())
        we = datetime.combine(last, datetime.max.time())
        for chunk in batched(insts, args.chunk):
            if time.monotonic() - started > args.budget:
                stop = True
                break
            t0 = time.monotonic()
            ranges = {
                str(i.instrument_id): gap_map[i.symbol]
                for i in chunk if i.symbol in gap_map
            }
            result = simple_sync(
                dhan, store, list(chunk), args.timeframe, ws, we, ranges=ranges,
            )
            written += result.written
            print(f"[repair] {first} [{bucket['kind']}]: {len(chunk)}/{len(insts)} symbols, "
                  f"{result.fetched} fetched, {result.written} rows, "
                  f"skipped={len(result.skipped)}, "
                  f"{time.monotonic() - t0:.1f}s", flush=True)
        done += 1
        if stop:
            break

    print(f"[repair] slice: {done}/{len(order)} clusters, written={written}, "
          f"{time.monotonic() - started:.0f}s"
          + (" (budget reached — rerun to continue)" if stop else ""), flush=True)
    remaining = scan()
    print(f"[repair] after: {remaining.gapped_symbols} symbols gapped, "
          f"open_missing={remaining.open_missing_symbols}, "
          f"close_missing={remaining.close_missing_symbols}", flush=True)
    for b in brokers.values():
        try:
            b.close()
        except Exception:  # noqa: BLE001 — teardown is best effort
            pass
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
