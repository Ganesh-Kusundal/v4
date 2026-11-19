"""A public top-level class/function name is defined in one module only.

Two modules defining the same name (``RecoveryResult`` in runtime and execution,
before 1b) make grep, imports and code review ambiguous. New collisions fail;
existing ones are listed below and must be removed from the list once resolved.
"""

from __future__ import annotations

import ast
import collections
import subprocess
from pathlib import Path

_ROOT = Path(__file__).resolve().parent.parent

# Known collisions. Intentional: per-broker mixins, per-app entry points, route
# handlers vs the use case they call. The rest are debt; shrink this set.
_ALLOWED = {
    "AdminMixin", "MarketDataMixin", "OrdersMixin", "PortfolioMixin",  # dhan/upstox parallel
    "main", "create_app", "health",  # one per deployable
    "cancel_order", "modify_order",  # route handler vs application use case
    "HistoryRequest", "IndicatorSpec", "StrategyRegistry", "as_decimal", "as_price",
    "get_session", "map_order_status", "parse_timestamp", "register_indicator",
}


def _collisions() -> dict[str, list[str]]:
    files = subprocess.check_output(
        ["git", "ls-files", "*/src/*.py"], cwd=_ROOT, text=True
    ).split()
    seen: dict[str, set[str]] = collections.defaultdict(set)
    for f in files:
        for n in ast.parse((_ROOT / f).read_text()).body:
            if isinstance(n, ast.ClassDef | ast.FunctionDef | ast.AsyncFunctionDef) and not n.name.startswith("_"):
                seen[n.name].add(f)
    return {k: sorted(v) for k, v in seen.items() if len(v) > 1}


def test_no_new_duplicate_toplevel_names() -> None:
    new = {k: v for k, v in _collisions().items() if k not in _ALLOWED}
    assert not new, f"name defined in more than one module: {new}"


def test_allow_list_has_no_stale_entries() -> None:
    stale = _ALLOWED - set(_collisions())
    assert not stale, f"resolved, remove from _ALLOWED: {sorted(stale)}"
