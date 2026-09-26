"""TradeX v4 trading platform.

Provides the reactive execution engine, SDK session, configuration, and runtime.

**Why this module imports so little.** ``tradex_trading`` is the composition
root and the *only* package allowed to import every other package. That
freedom made this ``__init__`` eagerly import ``tradex_runtime`` (for
``boot``/``TradingSession``/``StreamSubscription``) while
``tradex_runtime.startup`` in turn imports ``tradex_trading.sdk.session``.
Whichever package a process imported first, the second one re-entered the
first while it was still loading, so ``import tradex_runtime.session`` failed
on its own with::

    ImportError: cannot import name 'SessionState' from partially
    initialized module 'tradex_runtime.session'

The fix is to not create the cycle rather than to paper over it: everything
below is served lazily through PEP 562 ``__getattr__``, so importing
``tradex_trading`` no longer pulls in ``tradex_runtime``. The names are still
available exactly as before (``from tradex_trading import ExecutionEngine``
still works), and the import graph becomes a DAG again, which is what
``tests/test_import_boundaries.py::test_no_unknown_import_cycles_between_packages``
enforces.

Callers should prefer the defining package (``tradex_runtime.boot``,
``tradex_execution.ExecutionEngine``); these re-exports remain for the
convenience of existing callers and for the documented
``tradex_trading.*`` shim surface.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

if TYPE_CHECKING:  # pragma: no cover - import-time only, for type checkers
    from tradex_config.schema import AppConfig, RiskConfig
    from tradex_execution.engine import ExecutionEngine, RiskManager
    from tradex_execution.fill_sources import (
        BrokerFillSource,
        PaperFillSource,
        SimulatedFillSource,
    )
    from tradex_execution.trading_cache import TradingCache
    from tradex_reactive.bus import ReactiveBus
    from tradex_runtime.session import SessionState, TradingSession
    from tradex_runtime.startup import boot
    from tradex_runtime.streaming import StreamSubscription

#: Public name -> (defining module, attribute within that module).
#: Everything here is resolved on first attribute access, never at import.
_LAZY: dict[str, tuple[str, str]] = {
    "AppConfig": ("tradex_config.schema", "AppConfig"),
    "RiskConfig": ("tradex_config.schema", "RiskConfig"),
    "ExecutionEngine": ("tradex_execution.engine", "ExecutionEngine"),
    "RiskManager": ("tradex_execution.engine", "RiskManager"),
    "BrokerFillSource": ("tradex_execution.fill_sources", "BrokerFillSource"),
    "PaperFillSource": ("tradex_execution.fill_sources", "PaperFillSource"),
    "SimulatedFillSource": ("tradex_execution.fill_sources", "SimulatedFillSource"),
    "TradingCache": ("tradex_execution.trading_cache", "TradingCache"),
    "ReactiveBus": ("tradex_reactive.bus", "ReactiveBus"),
    "SessionState": ("tradex_runtime.session", "SessionState"),
    "TradingSession": ("tradex_runtime.session", "TradingSession"),
    "StreamSubscription": ("tradex_runtime.streaming", "StreamSubscription"),
    "boot": ("tradex_runtime.startup", "boot"),
}

__all__ = [
    "AppConfig",
    "BrokerFillSource",
    "ExecutionEngine",
    "PaperFillSource",
    "ReactiveBus",
    "RiskConfig",
    "RiskManager",
    "SessionState",
    "SimulatedFillSource",
    "StreamSubscription",
    "TradingCache",
    "TradingSession",
    "boot",
]


def __getattr__(name: str) -> object:
    """Resolve a re-exported name on first access (PEP 562)."""
    try:
        module_name, attr = _LAZY[name]
    except KeyError:
        raise AttributeError(
            f"module {__name__!r} has no attribute {name!r}"
        ) from None
    from importlib import import_module

    value = getattr(import_module(module_name), attr)
    # Cache on the module so subsequent lookups skip __getattr__ entirely.
    globals()[name] = value
    return value


def __dir__() -> list[str]:
    return sorted(set(globals()) | set(_LAZY))
