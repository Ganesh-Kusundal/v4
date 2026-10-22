"""Fail when the repo-wide mypy error count grows past ``.mypy_baseline``.

The tree carries known type debt, and some of it lives in untracked work in
progress that this branch must not edit. A hard wall that can never go green
makes every later CI step unreachable, so this is the same ratchet shape as
``scripts/ruff_ratchet.py``: the count may fall, it may not rise.

    uv run --no-sync python scripts/mypy_ratchet.py
    uv run --no-sync python scripts/mypy_ratchet.py --set-baseline
"""

from __future__ import annotations

import re
import subprocess
import sys
from pathlib import Path

_ROOT = Path(__file__).resolve().parent.parent
_BASELINE = _ROOT / ".mypy_baseline"

_TARGETS = (
    "domain/src/tradex_domain",
    "brokers/src/tradex_brokers",
    "config/src/tradex_config",
    "observability/src/tradex_observability",
    "analytics/src/tradex_analytics",
    "reactive/src/tradex_reactive",
    "execution/src/tradex_execution",
    "strategy/src/tradex_strategy",
    "replay/src/tradex_replay",
    "application/src/tradex_application",
    "market_data/src/tradex_market_data",
    "interfaces/src/tradex_interfaces",
    "runtime/src/tradex_runtime",
    "services/duckdb-analytics/src/duck_analytics",
)

_ERRORS = re.compile(r"^Found (\d+) errors? in ", re.MULTILINE)


def _run_mypy() -> tuple[int, str]:
    result = subprocess.run(
        [sys.executable, "-m", "mypy", *_TARGETS],
        cwd=_ROOT,
        capture_output=True,
        text=True,
    )
    output = result.stdout + result.stderr
    if "Success: no issues found" in output:
        return 0, output
    match = _ERRORS.search(output)
    if match is None:
        raise SystemExit(f"mypy emitted no summary line:\n{output}")
    return int(match.group(1)), output


def main() -> int:
    count, output = _run_mypy()
    print(output)
    if "--set-baseline" in sys.argv[1:]:
        _BASELINE.write_text(f"{count}\n", encoding="utf-8")
        print(f"mypy baseline set to {count}")
        return 0

    baseline = int(_BASELINE.read_text(encoding="utf-8").strip())
    if count > baseline:
        print(
            f"mypy errors grew: {baseline} -> {count}. "
            "Fix the new errors; do not raise the baseline.",
            file=sys.stderr,
        )
        return 1
    if count < baseline:
        print(
            f"mypy errors fell: {baseline} -> {count}. "
            "Record the improvement with --set-baseline.",
            file=sys.stderr,
        )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
