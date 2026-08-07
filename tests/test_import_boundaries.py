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
