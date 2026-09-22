"""Pin the single order-persistence seam (cleanup overhaul, phase 3).

The fold moved ``OrderStore``/``InMemoryOrderStore`` (ex-``order_store.py``)
and ``attach_order_persistence`` (ex-``order_persistence.py``) into
``sqlite_store.py``. These tests pin that decision: the names live in exactly
one module, and no source file imports the deleted module paths.
"""

from __future__ import annotations

import ast
from pathlib import Path

import tradex_trading.execution.sqlite_store as sqlite_store

_DELETED_MODULES = (
    "tradex_trading.execution.order_store",
    "tradex_trading.execution.order_persistence",
)

_FOLDED_NAMES = (
    "OrderStore",
    "InMemoryOrderStore",
    "attach_order_persistence",
    "SQLiteOrderStore",
)


def test_seam_exports_the_folded_names() -> None:
    for name in _FOLDED_NAMES:
        assert hasattr(sqlite_store, name), f"sqlite_store lost {name}"
        assert name in sqlite_store.__all__, f"{name} missing from __all__"


def test_no_source_imports_deleted_shims() -> None:
    src_root = Path(sqlite_store.__file__).resolve().parents[2]  # trading/src
    offenders: list[str] = []
    for path in sorted(src_root.rglob("*.py")):
        if "__pycache__" in path.parts:
            continue
        tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
        for node in ast.walk(tree):
            if isinstance(node, ast.ImportFrom) and node.module in _DELETED_MODULES:
                offenders.append(f"{path.name}:{node.lineno} from {node.module}")
            elif isinstance(node, ast.Import):
                for alias in node.names:
                    if alias.name in _DELETED_MODULES:
                        offenders.append(f"{path.name}:{node.lineno} import {alias.name}")
    assert not offenders, f"deleted persistence shims still imported: {offenders}"