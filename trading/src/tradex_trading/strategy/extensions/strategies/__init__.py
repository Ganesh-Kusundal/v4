"""User strategy classes — import each strategy module here.

Objects listed in ``__all__`` are validated against the runtime-checkable
``Strategy`` protocol by ``extensions/__init__.py``. Add a new strategy by
dropping a module in this package and importing it below.
"""

from tradex_trading.strategy.extensions.strategies.mean_reversion import (
    mean_reversion_strategy,
)
from tradex_trading.strategy.extensions.strategies.multi_symbol_sma_cross import (
    multi_symbol_sma_cross,
)
from tradex_trading.strategy.extensions.strategies.sma_cross import (
    sma_cross_strategy,
)

__all__ = [
    "mean_reversion_strategy",
    "multi_symbol_sma_cross",
    "sma_cross_strategy",
]
