"""Enforce the no-foreign-private-writes rule [REF-5, SMELL-04].

A module must never assign to an underscore attribute of an object it does
not own (``other._attr = ...``).  ``self._attr = ...`` inside the owning
class is fine.  Cross-object state changes must go through declared public
seams — this is what makes renames type-checkable and keeps layering honest.

Generated protobuf modules are exempt.
"""

from __future__ import annotations

import ast
from pathlib import Path

_ROOT = Path(__file__).resolve().parent.parent

# Every workspace package's ``src`` root. The scan originally covered only
# domain / brokers / trading and left the other 14 extracted packages unchecked,
# so a foreign private write anywhere in config, research, operations,
# observability, analytics, reactive, execution, strategy, replay,
# application, market_data, interfaces, runtime or persistence passed silently.
# All 17 roots are now scanned; keep this list in sync with the package set.
_SRC_DIRS = [
    _ROOT / "domain/src",
    _ROOT / "brokers/src",
    _ROOT / "trading/src",
    _ROOT / "config/src",
    _ROOT / "research/src",
    _ROOT / "operations/src",
    _ROOT / "observability/src",
    _ROOT / "analytics/src",
    _ROOT / "reactive/src",
    _ROOT / "execution/src",
    _ROOT / "strategy/src",
    _ROOT / "replay/src",
    _ROOT / "application/src",
    _ROOT / "market_data/src",
    _ROOT / "interfaces/src",
    _ROOT / "runtime/src",
    _ROOT / "persistence/src",
]


class _ForeignPrivateWriteChecker(ast.NodeVisitor):
    def __init__(self, path: Path) -> None:
        self.path = path
        self.violations: list[str] = []

    def _check(self, target: ast.expr, lineno: int) -> None:
        if not isinstance(target, ast.Attribute):
            return
        if not target.attr.startswith("_"):
            return
        # ``self._attr = ...`` inside the owning object is legitimate.
        if isinstance(target.value, ast.Name) and target.value.id == "self":
            return
        self.violations.append(
            f"{self.path.relative_to(_ROOT)}:{lineno}: assignment to foreign "
            f'private attribute "...{target.attr}"'
        )

    def visit_Assign(self, node: ast.Assign) -> None:  # noqa: N802
        for target in node.targets:
            self._check(target, node.lineno)
        self.generic_visit(node)

    def visit_AnnAssign(self, node: ast.AnnAssign) -> None:  # noqa: N802
        if node.value is not None:
            self._check(node.target, node.lineno)
        self.generic_visit(node)


def test_no_foreign_private_writes() -> None:
    violations: list[str] = []
    for src in _SRC_DIRS:
        for path in sorted(src.rglob("*.py")):
            if "__pycache__" in path.parts or path.name.endswith("_pb2.py"):
                continue
            tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
            checker = _ForeignPrivateWriteChecker(path)
            checker.visit(tree)
            violations.extend(checker.violations)
    assert not violations, "foreign private writes found:\n" + "\n".join(violations)
