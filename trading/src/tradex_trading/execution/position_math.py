"""Re-export position math from the domain kernel.

The canonical position-accounting functions (``apply_fill``, ``apply_split``,
``apply_dividend``) live in :mod:`tradex_domain.position_math` so both
``trading`` and ``brokers`` share one implementation (M8 fix — eliminates
the divergence risk from the duplicated paper-broker copy).

This module re-exports the domain functions plus the ``q2`` helper so
existing ``from tradex_trading.execution.position_math import …`` paths
continue to work without changes.
"""

from __future__ import annotations

from tradex_domain.position_math import apply_dividend, apply_fill, apply_split
from tradex_domain.utils import q2

__all__ = ["apply_dividend", "apply_fill", "apply_split", "q2"]
