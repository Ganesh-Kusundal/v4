"""Architecture contracts for strategy extensions (Phase 8).

An extension strategy is the leaf of the dependency graph: the code a user
drops into ``strategy/src/tradex_strategy/extensions/strategies/``. The
package-level rules in ``tests/test_import_boundaries.py`` cap
``tradex_strategy`` at ``{tradex_domain, tradex_analytics, tradex_market_data}``;
that is the right ceiling for the framework core, but it leaves two leaf-level
questions open, and this file answers them as contracts:

1. **No reach-around layers.** An extension strategy must not import
   ``tradex_execution``, ``tradex_brokers``, ``tradex_runtime`` or
   ``tradex_interfaces``. Each of those is a route around the one order path
   and the engine that owns it, so a leaf importing one is not a strategy any
   more.

2. **No concrete analytics.** Phase 2 built ``context.analytics`` (the
   ``AnalyticsView`` protocol in ``tradex_analytics.views``) so six strategies
   stop recomputing the same indicator six ways. An extension strategy reads
   indicators through that view. The only concrete import that is approved is
   ``tradex_analytics.indicators`` in the six study strategies that keep it as
   a *fallback* for standalone/unit-test use — the decision, its rationale and
   its scope are documented on ``_STRATEGY_EXT_ANALYTICS_FALLBACK`` in
   ``tests/test_import_boundaries.py``.

The scan helpers live in ``tests/test_import_boundaries.py`` (one source of
truth); these tests are the contract layer that states and exercises them.
"""

from __future__ import annotations

import importlib
import sys
from pathlib import Path

_ROOT = Path(__file__).resolve().parents[3]
_TESTS_DIR = str(_ROOT / "tests")
if _TESTS_DIR not in sys.path:
    sys.path.insert(0, _TESTS_DIR)

from test_import_boundaries import (  # noqa: E402
    _STRATEGY_EXT_ANALYTICS_CONTRACTS,
    _STRATEGY_EXT_ANALYTICS_FALLBACK,
    _STRATEGY_EXT_FORBIDDEN,
    _STRATEGY_EXT_SRC,
    _extension_concrete_analytics_hits,
    _extension_forbidden_layer_hits,
    _py_files,
)

#: The leaf-level reach-around ban, spelled out so a silent edit that drops a
#: layer fails here as well as in the scan.
_DECLARED_FORBIDDEN_LAYERS = {
    "tradex_execution",
    "tradex_brokers",
    "tradex_runtime",
    "tradex_interfaces",
}


def test_extension_forbidden_layers_are_the_declared_set() -> None:
    """The contract names exactly the four layers a strategy may not reach."""
    assert _STRATEGY_EXT_FORBIDDEN == _DECLARED_FORBIDDEN_LAYERS, (
        "the extension forbidden-layer set changed; update this contract and "
        "the rationale in tests/test_import_boundaries.py together"
    )


def test_extension_strategies_only_import_their_allowed_layers() -> None:
    """No extension strategy imports execution/brokers/runtime/interfaces."""
    offenders = _extension_forbidden_layer_hits()
    assert not offenders, (
        "extension strategies import a layer they must not reach; a strategy is "
        "a leaf and must go through the engine/ports, not around them:\n" + "\n".join(offenders)
    )


def test_extension_strategies_read_analytics_through_the_view_contract() -> None:
    """No extension strategy imports a concrete analytics implementation."""
    offenders = _extension_concrete_analytics_hits()
    assert not offenders, (
        "extension strategies import a concrete analytics implementation; read "
        "indicators through context.analytics (AnalyticsView) instead. The only "
        "exceptions are tradex_analytics.views and the documented fallback "
        "allowlist:\n" + "\n".join(offenders)
    )


def test_analytics_view_is_the_approved_contract() -> None:
    """``tradex_analytics.views`` is a Protocol, not an implementation.

    This is what makes it safe for a leaf to import: the dependency is on a
    shape, so the concrete runtime behind it can be swapped (cache, replay,
    test double) without touching a strategy.
    """
    module = importlib.import_module("tradex_analytics.views")
    analytics_view = module.AnalyticsView
    assert getattr(analytics_view, "_is_protocol", False), (
        "AnalyticsView is no longer a Protocol; the 'leaf imports a contract' "
        "argument no longer holds and the exception must be revisited"
    )
    assert callable(getattr(analytics_view, "indicator", None)), (
        "AnalyticsView.indicator disappeared; the seam strategies read is gone"
    )


def test_approved_analytics_contract_modules_exist() -> None:
    """Every approved contract module must still be importable."""
    for module_name in sorted(_STRATEGY_EXT_ANALYTICS_CONTRACTS):
        importlib.import_module(module_name)


def test_approved_fallback_is_a_fallback_not_the_only_path() -> None:
    """Each allowlisted file must also read the shared view.

    The fallback import is approved because it is the *second* path. If a file
    keeps the concrete import while dropping ``context.analytics``, the
    allowlist is no longer excusing a fallback — it is excusing the only path,
    which is exactly what Phase 2 removed.
    """
    for rel in sorted(_STRATEGY_EXT_ANALYTICS_FALLBACK):
        text = (_ROOT / rel).read_text(encoding="utf-8")
        assert "view.indicator(" in text, (
            f"{rel} is on the concrete-analytics fallback allowlist but no "
            "longer reads the shared analytics view; the fallback is now the "
            "only path, so drop the entry or restore the view path"
        )


def test_extension_contract_scan_roots_are_not_empty() -> None:
    """Guard the contract scans against passing vacuously."""
    assert _py_files(_STRATEGY_EXT_SRC), (
        f"no .py files found under {_STRATEGY_EXT_SRC.relative_to(_ROOT)}; the "
        "extension contract scan is not looking at anything"
    )
