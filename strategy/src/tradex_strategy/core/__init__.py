"""Strategy core — the framework itself.

Engine, protocols, scanner, and the reference strategy live here.
User-owned strategies and scanners live in ``strategy/extensions`` and are
auto-discovered — core files are never edited for user code.
"""

from tradex_strategy.core.buy_and_hold import BuyAndHoldStrategy
from tradex_strategy.core.engine import ReactiveStrategyEngine
from tradex_strategy.core.protocols import Strategy
from tradex_strategy.core.scanner import ScannerEngine

