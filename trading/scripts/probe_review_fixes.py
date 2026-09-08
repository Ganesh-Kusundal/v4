"""Non-production diagnostic for the retired execution-state review probes.

The original reviewer probes exercised the quarantined ``tradex_trading.events``
stack. That stack is legacy and must not be pulled into production or scripts.
The canonical v4 execution APIs have their own focused tests under
``trading/tests/execution`` and ``trading/tests/runtime``. This script therefore
keeps the probe entry point useful by checking the retirement boundary without
importing or executing the legacy stack.

Run from ``trading/``::

    python scripts/probe_review_fixes.py
"""
from __future__ import annotations

import ast
from pathlib import Path

PASS: list[str] = []
FAIL: list[str] = []


PRODUCTION_ROOTS = ("src", "scripts")
LEGACY_PREFIX = "tradex_trading.events"


def _imports_legacy_events(path: Path) -> list[str]:
    """Return legacy event imports found in *path* without importing the file."""
    try:
        tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
    except (OSError, SyntaxError):
        return []

    imports: list[str] = []
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            names = [alias.name for alias in node.names]
        elif isinstance(node, ast.ImportFrom):
            names = [node.module or ""]
        else:
            continue
        imports.extend(name for name in names if name == LEGACY_PREFIX or name.startswith(f"{LEGACY_PREFIX}."))
    return imports


def check_production_import_boundary(root: Path | None = None) -> tuple[bool, str]:
    """Verify production and script sources do not import the legacy events stack."""
    root = root or Path(__file__).resolve().parents[1]
    violations = [
        f"{path.relative_to(root)}: {imports}"
        for dirname in PRODUCTION_ROOTS
        for path in (root / dirname).rglob("*.py")
        if "tradex_trading/events" not in path.as_posix()
        and (imports := _imports_legacy_events(path))
    ]
    if violations:
        return False, "legacy events imports found: " + "; ".join(violations)
    return True, "canonical execution boundary is free of legacy events imports"


def main() -> int:
    ok, detail = check_production_import_boundary()
    name = "legacy events import boundary"
    (PASS if ok else FAIL).append(name)
    status = "PASS" if ok else "FAIL"
    print(f"[{status}] {name}: {detail}")
    print(f"diagnostic checks passed: {len(PASS)}/{len(PASS) + len(FAIL)}")
    return 0 if ok else 1


if __name__ == "__main__":
    raise SystemExit(main())
