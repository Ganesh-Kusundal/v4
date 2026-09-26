"""Compatibility shim — implementation lives in ``tradex_domain.universe``.

``load_universe`` / ``available_universes`` are pure domain functions (stdlib
+ ``tradex_domain`` only). They moved out of the datalake package on
2026-09-26 so ``tradex_strategy`` no longer has to import ``tradex_market_data``
for them, which broke the ``market_data -> replay -> strategy -> market_data``
import ring. This module is kept so the existing importers in
``market_data/__init__.py``, ``backtest_loader.py``, ``interfaces/cli.py``,
``interfaces/routes/chart.py`` and the trading scripts keep working unchanged.
New code should import ``tradex_domain.universe`` directly.
"""

from importlib import import_module as _import_module

_impl = _import_module("tradex_domain.universe")
globals().update(
    {k: v for k, v in vars(_impl).items() if not k.startswith("__")}
)
if hasattr(_impl, "__all__"):
    __all__ = list(_impl.__all__)
del _import_module, _impl
