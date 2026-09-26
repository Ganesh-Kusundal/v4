#!/usr/bin/env python3
"""Datalake coverage report — measure the lake, don't describe it from memory.

Why this exists: CLAUDE.md's "Datalake Facts" block carried hand-written
coverage numbers (``~261 MB``, ``~63+ trading days``) that had drifted to 745 MB
over 171 days with nothing failing anywhere. A number nobody can re-derive is a
number that is eventually wrong, so that block now points here instead of
asserting figures, and this script is the single source of truth for them.

Every figure below is computed from the parquet files on disk; nothing is
network-bound and nothing is written.

Two measurement choices, both about avoiding phantom gaps:

- **Density is judged within a day**, against that day's densest symbol, so the
  session length is derived from the data rather than assumed from a calendar.
- **Both session-edge stamps are counted apart from density.** The 09:15 open
  (Dhan's window is start-exclusive, so it is often absent) and the 15:30 close
  (neither broker's intraday series reaches it) are conventions, not data: if
  any symbol holds one, it becomes the day's "densest" and every other symbol is
  scored a bar short — ~500 phantom bars a day. Density is therefore measured
  over 09:16–15:29, with the two edges reported as their own counts. Skipping
  this is how a headline "11,555 bars missing" turned out to be mostly one
  convention stamp per symbol-day.

Usage::

    python trading/scripts/datalake_stats.py
    python trading/scripts/datalake_stats.py --json          # machine-readable
    python trading/scripts/datalake_stats.py --root /mnt/lake --top 10

``--root`` defaults to the repo-anchored root from ``datalake/paths.py``
(``$TRADEX_DATALAKE_ROOT`` when set), i.e. the same lake ``serve`` reads.

Exit codes: 0 for a report — an empty lake is a finding, not an error — and 2
when the lake was caught mid-write (a concurrent ``ParquetStorage.upsert``
rewrites its partitions in place), in which case re-run.
"""

from __future__ import annotations

import argparse
import json
import sys
from datetime import date, datetime, time, timedelta
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parent.parent.parent  # repo root
for sub in ("domain/src", "brokers/src", "trading/src"):
    sys.path.insert(0, str(ROOT / sub))

from tradex_domain.market_calendar import (  # noqa: E402 — sys.path bootstrap above
    MARKET_CLOSE,
    MARKET_CLOSE_STR,
    MARKET_OPEN,
    MARKET_OPEN_STR,
)

from tradex_trading.datalake.paths import datalake_root  # noqa: E402 — sys.path bootstrap above


def _shifted_str(base: time, minutes: int) -> str:
    """``base`` wall time shifted by *minutes*, as ``HH:MM:SS``."""
    return (datetime.combine(date(2026, 1, 1), base) + timedelta(minutes=minutes)).time().isoformat()


#: Session-edge stamps, straight from the canonical calendar.
OPEN_STR: str = MARKET_OPEN_STR
CLOSE_STR: str = MARKET_CLOSE_STR
#: The *interior* density band: deliberately one minute inside each session
#: edge, because 09:15 (Dhan's window is start-exclusive) and 15:30 (neither
#: broker's intraday series reaches it) are conventions rather than data. They
#: are counted apart, never as interior density.
INTERIOR_OPEN_STR: str = _shifted_str(MARKET_OPEN, 1)
INTERIOR_CLOSE_STR: str = _shifted_str(MARKET_CLOSE, -1)
#: Human-facing ``HH:MM`` labels for the same four stamps.
OPEN_LABEL: str = MARKET_OPEN.strftime("%H:%M")
CLOSE_LABEL: str = MARKET_CLOSE.strftime("%H:%M")
INTERIOR_OPEN_LABEL: str = datetime.strptime(INTERIOR_OPEN_STR, "%H:%M:%S").strftime("%H:%M")
INTERIOR_CLOSE_LABEL: str = datetime.strptime(INTERIOR_CLOSE_STR, "%H:%M:%S").strftime("%H:%M")

_PARQUET = "read_parquet(?, hive_partitioning=true)"

_SUMMARY_SQL = f"""
SELECT count(*) AS n_rows, count(DISTINCT symbol) AS symbols,
       min(timestamp) AS first_ts, max(timestamp) AS last_ts,
       count(DISTINCT CAST(timestamp AS DATE)) AS trading_days
FROM {_PARQUET}
"""

_BREAKDOWN_SQL = f"""
SELECT timeframe, kind, exchange, count(*) AS n
FROM {_PARQUET}
GROUP BY ALL
ORDER BY n DESC
"""

_PER_SYMBOL_SQL = f"""
WITH per_symbol AS (
    SELECT symbol,
           count(*) AS bars,
           count(DISTINCT CAST(timestamp AS DATE)) AS days,
           min(CAST(timestamp AS DATE)) AS first_day,
           max(CAST(timestamp AS DATE)) AS last_day
    FROM {_PARQUET}
    GROUP BY symbol
)
SELECT count(*) AS symbols,
       min(days) AS min_days,
       max(days) AS max_days,
       count(*) FILTER (WHERE days < (SELECT max(days) FROM per_symbol))
           AS symbols_below_max_days,
       -- Span is judged on *sessions*, not stamps: one symbol-day holding the
       -- intermittent 09:15 open would otherwise define the lake's first
       -- timestamp and make every other symbol look narrower than the lake.
       count(*) FILTER (
           WHERE first_day > (SELECT min(first_day) FROM per_symbol)
              OR last_day < (SELECT max(last_day) FROM per_symbol)
       ) AS symbols_narrower_span
FROM per_symbol
"""

# Session length per day = the interior bar count of that day's densest symbol.
# Anything smaller is a measured shortfall, never a calendar assumption. Both
# SESSION-EDGE stamps are excluded and counted on their own lines: the 09:15
# open (Dhan's window is start-exclusive, so it is often absent) and the 15:30
# close (neither broker's intraday series reaches it). Counting them as
# interior density is how "11,555 bars missing" mostly turned out to be one
# convention stamp per symbol-day.
_PER_DAY_SQL = f"""
WITH per_symbol AS (
    SELECT CAST(timestamp AS DATE) AS d,
           symbol,
           count(*) FILTER (
               WHERE CAST(timestamp AS TIME) BETWEEN TIME '{INTERIOR_OPEN_STR}' AND TIME '{INTERIOR_CLOSE_STR}'
           ) AS interior_bars,
           count(*) FILTER (WHERE CAST(timestamp AS TIME) = TIME '{OPEN_STR}')
               AS open_bars,
           count(*) FILTER (WHERE CAST(timestamp AS TIME) > TIME '{INTERIOR_CLOSE_STR}')
               AS close_bars
    FROM {_PARQUET}
    GROUP BY d, symbol
),
per_day AS (
    SELECT d,
           max(interior_bars) AS interior_len,
           count(*) AS syms,
           sum(interior_bars) AS interior_bars,
           count(*) FILTER (WHERE open_bars = 0) AS open_missing,
           count(*) FILTER (WHERE close_bars = 0) AS close_missing,
           max(interior_bars) * count(*) - sum(interior_bars) AS short_bars
    FROM per_symbol
    GROUP BY d
)
SELECT d, interior_len, syms, interior_bars, open_missing, close_missing, short_bars
FROM per_day
ORDER BY short_bars DESC, d
"""


def _human_bytes(n: int) -> str:
    """Format a byte count the way a human reads a lake's size on disk."""
    size = float(n)
    for unit in ("B", "KB", "MB", "GB", "TB"):
        if size < 1024 or unit == "TB":
            return f"{int(size)} B" if unit == "B" else f"{size:,.1f} {unit}"
        size /= 1024
    return f"{size:,.1f} TB"  # unreachable; keeps the type checker honest


def _fmt(ts: object) -> str | None:
    """Render a DuckDB timestamp as a stable, tz-naive IST wall-clock string."""
    return None if ts is None else str(ts)


def resolve_root(arg: str | None = None) -> Path:
    """Root to report on: ``--root``, else the lake ``serve`` itself reads.

    The default deliberately goes through ``paths.datalake_root()`` rather than
    a literal, so this reports on the repo-anchored lake whatever the cwd is —
    the same cwd-independence contract ``interface/routes`` is held to.
    """
    return Path(arg) if arg else Path(datalake_root())


def measure(root: Path) -> dict[str, Any]:
    """Build the coverage report for the lake at ``root`` (read-only).

    Returns a plain dict so ``--json`` and the text renderer share one shape.
    ``root`` is the store root in the ``ParquetStorage`` sense — the script
    nests ``ohlcv/`` under it, exactly like the store does.
    """
    import duckdb

    # Mirrors ParquetStorage.__init__: a root that already names the store is
    # used as-is, so ``--root data/ohlcv`` and ``--root data`` agree.
    ohlcv = root if root.name == "ohlcv" else root / "ohlcv"
    files = sorted(ohlcv.rglob("*.parquet"))
    report: dict[str, Any] = {
        "root": str(root),
        "ohlcv_root": str(ohlcv),
        "parquet_files": len(files),
        "bytes": sum(p.stat().st_size for p in files),
        "empty": not files,
    }
    if not files:
        return report

    glob = str(ohlcv / "**" / "*.parquet")
    con = duckdb.connect()
    try:
        rows, symbols, first_ts, last_ts, trading_days = con.execute(
            _SUMMARY_SQL, [glob]
        ).fetchone()
        report |= {
            "rows": int(rows),
            "symbols": int(symbols),
            "first_ts": _fmt(first_ts),
            "last_ts": _fmt(last_ts),
            "trading_days": int(trading_days),
        }

        report["breakdown"] = [
            {"timeframe": tf, "kind": kind, "exchange": ex, "rows": int(n)}
            for tf, kind, ex, n in con.execute(_BREAKDOWN_SQL, [glob]).fetchall()
        ]

        syms, min_days, max_days, below, narrower = con.execute(
            _PER_SYMBOL_SQL, [glob]
        ).fetchone()
        report |= {
            "min_days_per_symbol": int(min_days),
            "max_days_per_symbol": int(max_days),
            "symbols_below_max_days": int(below),
            "symbols_narrower_span": int(narrower),
        }

        days: list[dict[str, Any]] = [
            {
                "date": str(d),
                "interior_session_len": int(sl),
                "symbols": int(sy),
                "interior_bars": int(b),
                "open_bar_absent_symbol_days": int(om),
                "close_bar_absent_symbol_days": int(cm),
                "short_bars": int(sb),
            }
            for d, sl, sy, b, om, cm, sb in con.execute(_PER_DAY_SQL, [glob]).fetchall()
        ]
        short = [d for d in days if d["short_bars"] > 0]
        report |= {
            "days": len(days),
            "days_with_interior_shortfall": len(short),
            "interior_short_bars_total": sum(d["short_bars"] for d in short),
            "open_bar_absent_symbol_days": sum(
                d["open_bar_absent_symbol_days"] for d in days
            ),
            "close_bar_absent_symbol_days": sum(
                d["close_bar_absent_symbol_days"] for d in days
            ),
            "worst_days": short[:10],
            "interior_session_lengths": sorted(
                {d["interior_session_len"] for d in days}
            ),
        }
    finally:
        con.close()
    return report


def render(report: dict[str, Any], *, top: int = 5) -> str:
    """Render the report as an aligned, greppable text block."""
    out = [f"Datalake: {report['ohlcv_root']}"]
    if report["empty"]:
        out.append("  empty        no parquet files — run backfill_parquet.py "
                   "(or point --root/TRADEX_DATALAKE_ROOT at a populated lake)")
        return "\n".join(out)

    shape = ", ".join(
        f"{b['timeframe']} {b['kind']} {b['exchange']}"
        for b in report["breakdown"]
    )
    out.append(f"  files        {report['parquet_files']:,} parquet files, "
               f"{_human_bytes(report['bytes'])}")
    out.append(f"  shape        {report['rows']:,} rows · {report['symbols']} symbols "
               f"· {shape}")
    out.append(f"  range        {report['first_ts']} → {report['last_ts']}  "
               f"({report['trading_days']} trading days)")

    below = report["symbols_below_max_days"]
    span = report["symbols_narrower_span"]
    if below or span:
        out.append(f"  symbols      {below} below the {report['max_days_per_symbol']}-day "
                   f"max; {span} with a narrower span than the lake")
    else:
        out.append(f"  symbols      all {report['symbols']} cover all "
                   f"{report['max_days_per_symbol']} days")

    lens = report["interior_session_lengths"]
    sessions = f"{lens[0]}–{lens[-1]}" if len(lens) > 1 else str(lens[0])
    if report["days_with_interior_shortfall"]:
        out.append(f"  density      {report['interior_short_bars_total']:,} bars missing "
                   f"inside {INTERIOR_OPEN_LABEL}–{INTERIOR_CLOSE_LABEL} (vs each day's densest symbol, "
                   f"{sessions} bars when complete), on "
                   f"{report['days_with_interior_shortfall']} of {report['days']} days")
        for d in report["worst_days"][:top]:
            out.append(f"               {d['date']}  -{d['short_bars']:,}")
    else:
        out.append(f"  density      no interior shortfall — every symbol-day holds "
                   f"the full session ({sessions} bars after the open)")
    for label, count_key, stamp, why in (
        ("open bar ", "open_bar_absent_symbol_days", OPEN_LABEL,
         "Dhan's intraday window is start-exclusive"),
        ("close bar", "close_bar_absent_symbol_days", CLOSE_LABEL,
         "neither broker's intraday series reaches it"),
    ):
        absent = report[count_key]
        if not absent:
            continue
        noun = "symbol-day" if absent == 1 else "symbol-days"
        out.append(f"  {label}   {absent:,} {noun} missing the {stamp} bar "
                   f"({why}) — a session-edge stamp, counted here and not in "
                   "density")
    return "\n".join(out)


def main(argv: list[str] | None = None) -> int:
    p = argparse.ArgumentParser(
        description="Datalake coverage report (read-only; the docs carry no numbers)"
    )
    p.add_argument(
        "--root",
        default=None,
        help="Datalake root (default: repo-anchored data/, or $TRADEX_DATALAKE_ROOT)",
    )
    p.add_argument("--json", action="store_true", help="Machine-readable output")
    p.add_argument("--top", type=int, default=5,
                   help="How many short days to list (default 5)")
    args = p.parse_args(argv)

    root = resolve_root(args.root)
    try:
        report = measure(root)
    except Exception as exc:  # noqa: BLE001 — re-raised unless it is the mid-write case
        import duckdb

        if isinstance(exc, duckdb.IOException):
            print(f"[datalake-stats] lake is being written ({exc}); re-run")
            return 2
        raise

    if args.json:
        print(json.dumps(report, indent=2))
    else:
        print(render(report, top=args.top))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
