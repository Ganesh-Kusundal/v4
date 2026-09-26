"""Compatibility shim — implementation lives in ``tradex_domain.universe``.

``load_universe`` (and its sibling ``available_universes``) are pure domain
functions re-exported for the backfill/sync scripts and the datalake loader.
The implementation moved to ``tradex_domain``; this module (and
``tradex_market_data.universe``) forwards to it. These names used to be copied
via an opaque ``globals().update(...)`` that mypy cannot follow; the explicit
re-export below keeps the same public surface while being visible to mypy.
"""

from tradex_domain.universe import available_universes, load_universe

__all__ = ["load_universe", "available_universes"]
