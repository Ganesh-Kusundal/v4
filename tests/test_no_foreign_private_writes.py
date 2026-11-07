"""Enforce the no-foreign-private-writes rule [REF-5, SMELL-04].

A module must never assign to an underscore attribute of an object it does
not own (``other._attr = ...``).  ``self._attr = ...`` inside the owning
class is fine.  Cross-object state changes must go through declared public
seams — this is what makes renames type-checkable and keeps layering honest.

Generated protobuf modules are exempt.
"""

from __future__ import annotations

import ast
import tomllib
from pathlib import Path

_ROOT = Path(__file__).resolve().parent.parent


def _workspace_src_dirs() -> list[Path]:
    data = tomllib.loads((_ROOT / "pyproject.toml").read_text(encoding="utf-8"))
    members = data["tool"]["uv"]["workspace"]["members"]
    dirs = [_ROOT / member / "src" for member in members]
    return sorted(path for path in dirs if path.is_dir())


_SRC_DIRS = _workspace_src_dirs()


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


def test_the_scan_covers_every_workspace_source_root() -> None:
    # Hardcoded expected set — prevents silent scan shrinkage.
    # If the scan logic changes and stops covering a workspace src directory,
    # this assertion will fail rather than passing because both sides would
    # derive from the same dynamic expression.
    #
    # Current workspace members (from pyproject.toml [tool.uv.workspace].members)
    # that have a src/ directory:
    #   domain, brokers, config, observability, analytics, reactive, execution,
    #   strategy, replay, application, market_data, interfaces, runtime,
    #   services/duckdb-analytics, gateway
    expected = {
        _ROOT / "domain/src",
        _ROOT / "brokers/src",
        _ROOT / "config/src",
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
        _ROOT / "services/duckdb-analytics/src",
        _ROOT / "gateway/src",
    }
    assert set(_SRC_DIRS) == expected, (
        f"scan covers {len(_SRC_DIRS)} roots but expected {len(expected)}; "
        f"missing={expected - set(_SRC_DIRS)}, extra={set(_SRC_DIRS) - expected}"
    )
    assert all(path.is_dir() for path in _SRC_DIRS), "a scan root does not exist"
