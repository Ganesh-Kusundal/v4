"""User-owned strategy extensions — auto-discovered, core never edited.

Discovery contract (pinned): each ``extensions`` sub-package lists candidate
objects in ``__all__``; this package filters them with ``isinstance`` against
the runtime-checkable ``Strategy`` protocol and the ``ScannerDefinition``
dataclass, exposing ``all_strategies`` and ``all_scanners``.

Importing this package (directly, or via ``tradex_trading.strategy``) triggers
discovery — custom strategies and scanners are picked up without touching core.
"""

from __future__ import annotations

from typing import Any, TypeVar, cast

from tradex_domain.strategy import ScannerDefinition

from tradex_trading.strategy.core.protocols import Strategy
from tradex_trading.strategy.extensions import scanners as _scanners
from tradex_trading.strategy.extensions import shared as _shared  # noqa: F401
from tradex_trading.strategy.extensions import strategies as _strategies

_T = TypeVar("_T")


def _collect(package: object, predicate: Any) -> tuple[_T, ...]:
    """Gather objects from *package*'s ``__all__`` that satisfy *predicate*.

    Names missing from the package namespace are skipped (not fatal) so a
    typo in a user's ``__all__`` degrades to "not discovered" instead of
    breaking every strategy import.
    """
    found: list[_T] = []
    for name in getattr(package, "__all__", ()):
        obj = getattr(package, name, None)
        if obj is not None and isinstance(obj, predicate):
            found.append(cast(_T, obj))
    return tuple(found)


all_strategies: tuple[Strategy, ...] = _collect(_strategies, Strategy)
all_scanners: tuple[ScannerDefinition, ...] = _collect(_scanners, ScannerDefinition)

__all__ = ["all_strategies", "all_scanners"]
