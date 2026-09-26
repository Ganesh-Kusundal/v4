"""Public re-exports must survive: the app can break while tests stay green.

``execution.engine`` and ``recovery`` re-export names that other modules and
tests import *through* them (``startup`` imports ``MemoryIdempotencyGuard`` from
``engine``). An unused-import cleanup strips those names, and because nothing
in the unit suite exercises the real import graph, the suite keeps passing while
the server no longer starts. This pins the contract at the module boundary.
"""

from __future__ import annotations

import importlib

import pytest

#: module -> names other code imports from it.
PUBLIC_SURFACE: dict[str, tuple[str, ...]] = {
    "tradex_execution.engine": (
        "ExecutionEngine",
        "RiskManager",
        "RiskCheckResult",
        "OrderStore",
        "InMemoryOrderStore",
        "MemoryIdempotencyGuard",
        "IdempotencyDuplicate",
        "IdempotencyKeyReuseMismatch",
    ),
    "tradex_execution.recovery": (
        "recover_trading_cache",
        "fold_cash",
        "CashState",
        "CashStateUnknownError",
        "InMemoryEventStore",
        "SQLiteEventStore",
    ),
    "tradex_execution.risk": ("RiskManager", "RiskCheckResult"),
    "tradex_execution.projection": ("execution_projection",),
}


@pytest.mark.parametrize(
    ("module_name", "symbol"),
    [
        (module_name, symbol)
        for module_name, symbols in PUBLIC_SURFACE.items()
        for symbol in symbols
    ],
)
def test_public_symbol_is_importable(module_name: str, symbol: str) -> None:
    """A stripped re-export breaks the app, not the unit suite."""
    module = importlib.import_module(module_name)
    assert hasattr(module, symbol), (
        f"{module_name}.{symbol} is part of the public surface and was removed "
        f"— callers import it through this module"
    )


def test_the_application_import_graph_resolves() -> None:
    """Import the real composition root, not just the leaf modules.

    The unit suite builds engines directly, so it never exercises the imports
    ``boot()`` performs. A broken chain here is exactly the failure that let a
    stripped re-export reach a green test run.
    """
    from tradex_config.schema import AppConfig
    from tradex_interfaces.fastapi_app import create_app
    from tradex_runtime.startup import boot

    assert callable(boot)
    assert create_app() is not None
    assert AppConfig().mode == "paper"
