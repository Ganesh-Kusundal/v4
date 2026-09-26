"""Strategy module — reactive strategy engine, protocols, scanners, extensions.

Framework code lives in ``tradex_trading.strategy.core``; user-owned strategies
and scanners live in ``tradex_trading.strategy.extensions`` and are
auto-discovered on import (see ``all_strategies`` / ``all_scanners``).
"""

from tradex_strategy.core import (
    BuyAndHoldStrategy,
    ReactiveStrategyEngine,
    ScannerEngine,
    Strategy,
)
from tradex_strategy.extensions import all_scanners, all_strategies

