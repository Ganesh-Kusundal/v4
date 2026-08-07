"""Strategy core — the framework itself.

Engine, protocols, scanner, ensemble, and the reference strategy live here.
User-owned strategies and scanners live in ``strategy/extensions`` and are
auto-discovered — core files are never edited for user code.
"""

from tradex_trading.strategy.core.buy_and_hold import BuyAndHoldStrategy
from tradex_trading.strategy.core.engine import ReactiveStrategyEngine
from tradex_trading.strategy.core.ensemble import StrategyEnsemble, StrategyEntry
from tradex_trading.strategy.core.protocols import Strategy
from tradex_trading.strategy.core.scanner import ScannerEngine

__all__ = [
    "Strategy",
    "ReactiveStrategyEngine",
    "ScannerEngine",
    "BuyAndHoldStrategy",
    "StrategyEnsemble",
    "StrategyEntry",
]
