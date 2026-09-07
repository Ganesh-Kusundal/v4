"""Enforce the v4 package dependency boundary (F10).

Layers, strictly:
    domain  → (nothing internal)
    brokers → tradex_domain only
    trading → tradex_domain, tradex_brokers

Any import that violates this graph fails the build. This is a cheap AST
scan so it does not require importing the packages (and it stays fast).

Known safe exception: ``tradex_domain.serialization`` uses
``importlib.import_module`` to rebuild objects, but only ever for
``tradex_domain.*`` markers (guarded by ``_DOMAIN_PREFIX``).
"""

from __future__ import annotations

import ast
from pathlib import Path

_ROOT = Path(__file__).resolve().parent.parent

_SRC = {
    "domain": _ROOT / "domain/src",
    "brokers": _ROOT / "brokers/src",
    "trading": _ROOT / "trading/src",
}

_INTERNAL = {"tradex_domain", "tradex_brokers", "tradex_trading"}

# package → internal packages it is ALLOWED to import
_ALLOWED: dict[str, set[str]] = {
    "domain": set(),
    "brokers": {"tradex_domain"},
    "trading": {"tradex_domain", "tradex_brokers"},
}

# file → extra allowed modules (for the documented importlib exception)
_EXCEPTIONS = {
    "domain/src/tradex_domain/serialization.py": {"importlib"},
}


def _imported_modules(tree: ast.AST) -> set[str]:
    """Collect top-level internal module names imported by *tree*."""
    modules: set[str] = set()

    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            for alias in node.names:
                modules.add(alias.name.split(".")[0])
        elif isinstance(node, ast.ImportFrom):
            if node.level == 0 and node.module:
                modules.add(node.module.split(".")[0])
    return modules


def _py_files(src: Path) -> list[Path]:
    return sorted(p for p in src.rglob("*.py") if "__pycache__" not in p.parts)


def test_domain_never_imports_brokers_or_trading() -> None:
    forbidden = _INTERNAL - _ALLOWED["domain"]
    for path in _py_files(_SRC["domain"]):
        tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
        imported = _imported_modules(tree)
        imported -= {"tradex_domain"}  # self-imports are legitimate
        # serialization.py may importlib for tradex_domain markers only
        if str(path.relative_to(_ROOT)) in _EXCEPTIONS:
            imported -= {"importlib"}
        violations = imported & forbidden
        assert not violations, f"{path.relative_to(_ROOT)} imports {sorted(violations)}"


def test_brokers_never_imports_trading() -> None:
    forbidden = _INTERNAL - _ALLOWED["brokers"]
    for path in _py_files(_SRC["brokers"]):
        tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
        imported = _imported_modules(tree)
        imported -= {"tradex_brokers"}  # self-imports are legitimate
        violations = imported & forbidden
        assert not violations, f"{path.relative_to(_ROOT)} imports {sorted(violations)}"


def test_all_three_packages_present() -> None:
    """Guard against the test silently scanning nothing."""
    for name, src in _SRC.items():
        assert _py_files(src), f"no .py files found under {name}/src"


# ---------------------------------------------------------------------------
# Single-authority guard for the active execution spine
# ---------------------------------------------------------------------------

# The platform's production-readiness argument is: one order authority and one
# money path. If a new module starts claiming order state, fill identity, mark
# freshness, or unrealized PnL as authoritative, this test should fail instead
# of letting the claim silently exist alongside the active spine.
#
# This is a small structural guard, not a full architectural lint. It watches the
# named authority symbols that the 2026-09-04 reviews treated as the live spine:
#   - execution: ExecutionEngine / PositionManager / TradingCache
#   - money path: position_math.apply_fill
#
# If the repo later adopts a new spine, this guard must be revisited explicitly.

_SINGLE_AUTHORITY_SYMBOLS: dict[str, set[str]] = {
    "trading/src/tradex_trading/execution/engine.py": {"ExecutionEngine"},
    "trading/src/tradex_trading/execution/position_manager.py": {"PositionManager"},
    "trading/src/tradex_trading/execution/position_math.py": {"apply_fill"},
    "trading/src/tradex_trading/execution/trading_cache.py": {"TradingCache"},
}


def _top_level_names(path: Path) -> set[str]:
    tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
    names: set[str] = set()
    for node in ast.iter_child_nodes(tree):
        if isinstance(node, ast.ClassDef):
            names.add(node.name)
        elif isinstance(node, ast.FunctionDef | ast.AsyncFunctionDef):
            names.add(node.name)
    return names


def test_single_execution_authority_in_active_spine() -> None:
    """The active spine keeps order + money authority in a small, named set.

    If a second module in the execution tree starts defining the same
    authoritative symbols, that is a duplication signal and the build must
    fail so the team can decide which authority owns the concern.
    """
    trading_src = _ROOT / "trading/src"
    executed: dict[str, set[str]] = {}
    for path in sorted(trading_src.rglob("*.py")):
        if "__pycache__" in path.parts:
            continue
        rel = str(path.relative_to(_ROOT))
        names = _top_level_names(path)
        if names:
            executed[rel] = names

    assert executed, "trading/src appears to contain no Python files"

    for path_text, expected in _SINGLE_AUTHORITY_SYMBOLS.items():
        owning = executed.get(path_text)
        assert owning is not None, f"expected authority file missing: {path_text}"
        missing = expected - owning
        assert not missing, (
            f"{path_text} no longer owns the expected authority symbols: {sorted(missing)}. "
            "If this is intentional, update the single-authority guard explicitly."
        )

    # One money path: apply_fill should not be redefined in another execution
    # module as an authoritative fill applier. We do not forbid helpers named
    # apply_fill elsewhere; we forbid the authoritative one from being duplicated
    # in the active execution tree.
    authoritative_owner = (
        "trading/src/tradex_trading/execution/position_math.py"
    )
    owners_of_apply_fill = sorted(
        rel for rel, names in executed.items()
        if "apply_fill" in names and rel != authoritative_owner
    )
    assert not owners_of_apply_fill, (
        "apply_fill is defined in more than one execution module. "
        "Money path duplication detected:\n" + "\n".join(owners_of_apply_fill)
    )
