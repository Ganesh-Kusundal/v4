"""Compatibility shim — implementation lives in ``tradex_domain.universe``.

``load_universe`` / ``available_universes`` are pure domain functions (stdlib
+ ``tradex_domain`` only). They moved out of the datalake package on
2026-09-26 so ``tradex_strategy`` no longer has to import ``tradex_market_data``
for them, which broke the ``market_data -> replay -> strategy -> market_data``
import ring. This module is kept so the existing importers in
``market_data/__init__.py``, ``interfaces/cli.py``,
``interfaces/routes/chart.py`` and the trading scripts keep working unchanged.
New code should import ``tradex_domain.universe`` directly.

This used to forward dynamically, via::

    globals().update({k: v for k, v in vars(_impl).items() if not k.startswith("__")})

which mypy cannot follow, so every consumer of this module reported a false
``Module "tradex_market_data.universe" has no attribute "load_universe"``
``[attr-defined]``. The re-exports below are explicit, so the same names are
visible to mypy *and* to the runtime, and they are bound at import time exactly
as the dynamic copy bound them — patching ``tradex_domain.universe.load_universe``
never updated this module, and still does not.
"""

from tradex_domain.universe import available_universes, load_universe

__all__ = ["load_universe", "available_universes"]
