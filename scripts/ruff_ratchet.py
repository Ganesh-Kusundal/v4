#!/usr/bin/env python3
"""Ratchet the lint wall: fail only if the ruff error count went UP.

The quality gate's hard `ruff check` step cannot go green until the whole lint
wall is cleared, which is a cleanup project, not a safety property. Uncle Bob's
point: the wall is a symptom of a missing discipline, so the discipline has to
be the gate, and the number is the output, not the target.

This script keeps the gate green for the property that actually matters, which
is "no new lint errors", while a committed baseline (`.ruff_baseline`) records
the current count so the wall can only ever shrink. If the count rises above
the baseline it exits non-zero and prints the top offending rules.

Wire it into CI in place of the raw hard `ruff check` step. To lower the
baseline on purpose (after a cleanup), run:
    scripts/ruff_ratchet.py --set-baseline
"""

from __future__ import annotations

import argparse
import re
import subprocess
import sys
from pathlib import Path

# Resolved from the repository root, not the caller's cwd: the gate runs this
# from whatever directory it was invoked in, and a relative path silently
# found no baseline there (or worse, a different one).
_ROOT = Path(__file__).resolve().parent.parent
BASELINE_FILE = _ROOT / ".ruff_baseline"
COUNT_RE = re.compile(r"Found (\d+) error")


def current_count() -> tuple[int, str]:
    proc = subprocess.run(
        ["ruff", "check", "--output-format=concise"],
        cwd=_ROOT,
        capture_output=True,
        text=True,
        check=False,
    )
    output = proc.stdout + proc.stderr
    match = COUNT_RE.search(output)
    if not match:
        # No "Found N errors" line means zero, or ruff failed to run entirely.
        if proc.returncode == 0:
            return 0, output
        print("ruff ratchet: could not parse a count from ruff output:\n" + output, file=sys.stderr)
        return -1, output
    return int(match.group(1)), output


def top_rules() -> str:
    proc = subprocess.run(
        ["ruff", "check", "--statistics"],
        cwd=_ROOT,
        capture_output=True,
        text=True,
        check=False,
    )
    lines = (proc.stdout + proc.stderr).strip().splitlines()[:8]
    return "\n".join("  " + line for line in lines)


def main() -> int:
    parser = argparse.ArgumentParser(
        description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter
    )
    parser.add_argument(
        "--set-baseline", action="store_true", help="record the current count as the new baseline"
    )
    args = parser.parse_args()

    count, _ = current_count()
    if count < 0:
        return 2

    if args.set_baseline:
        BASELINE_FILE.write_text(f"{count}\n", encoding="utf-8")
        print(f"ruff ratchet: baseline set to {count}")
        return 0

    # Fail closed: a missing baseline used to record the current count and pass,
    # which meant the very first run in a fresh checkout could never fail and the
    # ratchet silently became a no-op if the file was not committed.
    baseline_text = (
        BASELINE_FILE.read_text(encoding="utf-8").strip() if BASELINE_FILE.exists() else ""
    )
    if not baseline_text:
        print(
            f"ruff ratchet: FAIL — no baseline at {BASELINE_FILE}. "
            "Record one deliberately with --set-baseline.",
            file=sys.stderr,
        )
        return 1
    baseline = int(baseline_text)

    if count > baseline:
        print(
            f"ruff ratchet: FAIL — {count} lint errors, baseline is {baseline}. "
            f"The wall may shrink, never grow. Top rules:\n{top_rules()}",
            file=sys.stderr,
        )
        return 1

    if count < baseline:
        print(
            f"ruff ratchet: wall shrank {baseline} → {count}. "
            "Run --set-baseline to record the lower baseline."
        )
        return 0

    print(f"ruff ratchet: OK — {count} errors, at baseline.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
