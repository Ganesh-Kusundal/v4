"""CLI runner — run any canned screener from the terminal.

Usage::

    python -m duck_analytics.screener --scanner topgainer --as-of "2026-08-21 09:45:00"
    python -m duck_analytics.screener --list
"""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

from duck_analytics.catalog import DuckDBCatalog, default_config_for
from duck_analytics.query import QueryService
from duck_analytics.scanners import breadth, scan_screener

_REPO_HINT = Path(__file__).resolve().parents[3]  # .../v4


def _build_registry() -> dict:
    return {
        "screener": (scan_screener, ["as_of"]),   # THE screener
        "breadth": (breadth, ["as_of"]),          # market context
    }


def main(argv: list[str] | None = None) -> int:
    registry = _build_registry()
    ap = argparse.ArgumentParser(prog="screener", description=__doc__)
    ap.add_argument("--scanner", choices=sorted(registry), default="screener")
    ap.add_argument("--as-of", required=True,
                    help="decision point, e.g. '2026-08-21 09:45:00'")
    ap.add_argument("--limit", type=int, default=30)
    args = ap.parse_args(argv)

    fn = registry[args.scanner][0]
    q = fn(args.as_of)
    cfg = default_config_for(_REPO_HINT)
    cat = DuckDBCatalog(cfg)
    try:
        res = QueryService(cat, cfg).execute(q.sql, None, require_complete=True)
    finally:
        cat.close()

    print(f"== {args.scanner} @ {args.as_of} ==  {q.description}")
    print(f"signals: {res.row_count}"
          + (" (TRUNCATED)" if res.truncated else "")
          + f"  [{res.elapsed_ms} ms]")
    if not res.rows:
        return 0
    widths = [max(len(str(c)), *(len(str(r[i])) for r in res.rows))
              for i, c in enumerate(res.columns)]
    print("  ".join(str(c).ljust(w) for c, w in zip(res.columns, widths, strict=True)))
    for row in res.rows[: args.limit]:
        print("  ".join(str(v).ljust(w) for v, w in zip(row, widths, strict=True)))
    return 0


if __name__ == "__main__":
    sys.exit(main())
