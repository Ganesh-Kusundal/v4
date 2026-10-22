"""One package tree: no un-installable `tradex_shared` shim."""

from __future__ import annotations

from pathlib import Path

_ROOT = Path(__file__).resolve().parent.parent
# This file must state the banned name, so the scan below skips itself.
_SELF = Path(__file__).resolve()


def test_the_shared_tree_is_gone() -> None:
    assert not (_ROOT / "shared").exists(), (
        "shared/src was never a workspace member; it moved into tradex_domain"
    )


def test_nothing_imports_tradex_shared() -> None:
    offenders: list[str] = []
    for path in _ROOT.rglob("*.py"):
        if "__pycache__" in path.parts or ".venv" in path.parts:
            continue
        if path.resolve() == _SELF:
            continue
        if "tradex_shared" in path.read_text(encoding="utf-8", errors="ignore"):
            offenders.append(str(path.relative_to(_ROOT)))
    assert not offenders, f"tradex_shared still referenced by: {offenders}"
