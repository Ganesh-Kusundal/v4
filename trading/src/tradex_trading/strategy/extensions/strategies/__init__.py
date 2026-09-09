"""User strategy classes — import each strategy module here.

Objects listed in ``__all__`` are validated against the runtime-checkable
``Strategy`` protocol by ``extensions/__init__.py``. Add a new strategy by
dropping a module in this package and importing it below.
"""

from tradex_trading.strategy.extensions.strategies.bollinger_breakout import (
    bollinger_breakout_strategy,
)
from tradex_trading.strategy.extensions.strategies.ema_ribbon_pullback import (
    ema_ribbon_pullback_strategy,
)
from tradex_trading.strategy.extensions.strategies.macd_cross import (
    macd_cross_strategy,
)
from tradex_trading.strategy.extensions.strategies.mean_reversion import (
    mean_reversion_strategy,
)
from tradex_trading.strategy.extensions.strategies.multi_symbol_sma_cross import (
    multi_symbol_sma_cross,
)
from tradex_trading.strategy.extensions.strategies.rsi_reversal import (
    rsi_reversal_strategy,
)
from tradex_trading.strategy.extensions.strategies.sma_cross import (
    sma_cross_strategy,
)
from tradex_trading.strategy.extensions.strategies.supertrend_flip import (
    supertrend_flip_strategy,
)

__all__ = [
    "bollinger_breakout_strategy",
    "ema_ribbon_pullback_strategy",
    "macd_cross_strategy",
    "mean_reversion_strategy",
    "multi_symbol_sma_cross",
    "rsi_reversal_strategy",
    "sma_cross_strategy",
    "supertrend_flip_strategy",
]
