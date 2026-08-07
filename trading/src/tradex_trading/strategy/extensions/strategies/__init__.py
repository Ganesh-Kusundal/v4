"""User strategy classes — import each strategy module here.

Objects listed in ``__all__`` are validated against the runtime-checkable
``Strategy`` protocol by ``extensions/__init__.py``. Add a new strategy by
dropping a module in this package and importing it below.
"""

from tradex_trading.strategy.extensions.strategies.sma_cross import (
    sma_cross_strategy,
)

__all__ = ["sma_cross_strategy"]
