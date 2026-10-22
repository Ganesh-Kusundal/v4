#!/usr/bin/env python3
"""Per-package BRANCH coverage floors, ratcheting upward only.

Why this exists
---------------
The gate ran one aggregate number, ``--cov-fail-under=85`` on **line**
coverage. That has two failure modes, both of which this repository had already
hit:

1. An aggregate hides the spread. A package at 60% sitting beside five at 100%
   is invisible, so a module like ``position_accountant`` could sit under-covered
   indefinitely while the headline number looked healthy.
2. Line coverage counts ``if x:`` as covered whether the other side is ever
   taken. On a money path the branches *are* the arithmetic: order status, fill
   direction, fee tier, the pass and fail arms of a risk gate. Line coverage
   cannot see any of that.

So this asserts **branch** coverage per target package, which is the number the
review said was disabled (``has_arcs = 0`` in the old ``.coverage``).

Floors are set two points below the measured value and may only rise. Ratchet
with::

    scripts/coverage_floors.py --json <coverage.json> --set-baseline

Usage in CI, after the pytest step that writes the report::

    scripts/coverage_floors.py --json coverage.json
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

#: Baseline. Two points under each measured value as of 2026-09-27, so the floors
#: pass today and every real improvement can raise them. `tradex_ingest` is the
#: weakest of the five, which is expected: it holds the broker adapters and the
#: parquet store, the two least-testable surfaces in the platform.
BASELINE: dict[str, int] = {
    "tradex_core": 77,
    "tradex_ingest": 67,
    "tradex_app": 76,
    "tradex_domain": 88,
    "tradex_config": 76,
}

#: Current packages -> the target package each becomes under spec 0002 Wave 4.
#: Asserted against the five target packages rather than the seventeen current
#: ones, because the target layout is the thing being protected and Wave 4's gate
#: is where the boundary test starts enforcing it.
_MEMBERS: dict[str, list[str]] = {
    "tradex_core": [
        "tradex_execution",
        "tradex_application",
        "tradex_reactive",
        "tradex_observability",
        "tradex_analytics",
    ],
    "tradex_ingest": ["tradex_brokers", "tradex_market_data"],
    "tradex_app": [
        "tradex_runtime",
        "tradex_interfaces",
        "tradex_strategy",
        "tradex_replay",
    ],
    "tradex_domain": ["tradex_domain"],
    "tradex_config": ["tradex_config"],
}

#: `services/duckdb-analytics` is a separate workspace member, exempt from the
#: package boundary test by decision, and not part of any of the five targets.
#: It is measured here anyway, at a floor that passes today (57.9% branch), so
#: it cannot quietly regress while being "not part of the architecture".
_DUCKDB_PATH = "services/duckdb-analytics/src/duck_analytics/"


def _measure(report: dict) -> dict[str, list[int]]:
    """Return {package: [missing_lines, covered_lines, missing_branches, covered_branches]}."""
    out: dict[str, list[int]] = {}
    for path, entry in report.get("files", {}).items():
        pkg = None
        if path.startswith(_DUCKDB_PATH):
            pkg = "duckdb_analytics"
        else:
            head, _, rest = path.partition("/src/")
            if head and rest.startswith("tradex_"):
                pkg = rest.split("/", 1)[0]
        if pkg is None:
            continue
        s = entry["summary"]
        acc = out.setdefault(pkg, [0, 0, 0, 0])
        acc[0] += s["missing_lines"]
        acc[1] += s["covered_lines"]
        acc[2] += s["missing_branches"]
        acc[3] += s["covered_branches"]
    return out


def _pct(covered: int, total: int) -> float:
    return 100.0 * covered / total if total else 100.0


def evaluate(report: dict) -> list[tuple[str, float, int]]:
    """Return [(target, branch_pct, floor)] for every target, plus duckdb."""
    per_pkg = _measure(report)
    rows: list[tuple[str, float, int]] = []
    for target, members in _MEMBERS.items():
        bm = bc = 0
        for member in members:
            got = per_pkg.get(member)
            if got is None:
                continue
            bm += got[2]
            bc += got[3]
        rows.append((target, _pct(bc, bc + bm), BASELINE[target]))
    duck = per_pkg.get("duckdb_analytics")
    if duck is not None:
        rows.append(("duckdb_analytics", _pct(duck[3], duck[2] + duck[3]), 55))
    return rows


def main() -> int:
    ap = argparse.ArgumentParser(
        description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter
    )
    ap.add_argument("--json", required=True, type=Path, help="coverage json report")
    ap.add_argument(
        "--set-baseline", action="store_true", help="rewrite BASELINE from measured values"
    )
    args = ap.parse_args()

    if not args.json.exists():
        print(f"coverage floors: no report at {args.json}", file=sys.stderr)
        return 2
    report = json.loads(args.json.read_text(encoding="utf-8"))
    rows = evaluate(report)

    if args.set_baseline:
        print("coverage floors: new baseline would be")
        for name, pct, _ in rows:
            print(f"  {name}: {int(pct)}")
        print("\nEdit BASELINE in this file deliberately; it is not rewritten automatically.")
        return 0

    failures: list[str] = []
    print("branch coverage by target package:")
    for name, pct, floor in rows:
        mark = "ok " if pct >= floor else "FAIL"
        print(f"  {mark} {name:<18} {pct:>5.1f}%  floor {floor}")
        if pct < floor:
            failures.append(f"{name} is {pct:.1f}% branch coverage, floor is {floor}")

    if failures:
        print("\ncoverage floors: FAIL\n  " + "\n  ".join(failures), file=sys.stderr)
        print(
            "\nIf coverage genuinely fell, that is the gate working. If a floor is "
            "wrong, raise it in BASELINE with a reason, deliberately.",
            file=sys.stderr,
        )
        return 1

    print("\ncoverage floors: OK — every target package is at or above its floor.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
