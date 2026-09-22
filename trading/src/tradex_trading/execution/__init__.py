"""Execution engine and OMS for the TradeX v4 trading platform.

Public intake interface: the engine, fill sources, fee calculation,
order/position management, and the trading cache.  Internal types
(idempotency guards, the sqlite persistence seam, risk internals) live
in their respective submodules — import from there directly.
"""

from tradex_trading.execution.engine import ExecutionEngine, RiskManager
from tradex_trading.execution.fees import FeeCalculator, PricingService
from tradex_trading.execution.fill_sources import (
    BrokerFillSource,
    FillSource,
    PaperFillSource,
    ReplayFillSource,
    SimulatedFillSource,
)
from tradex_trading.execution.order_manager import OrderManager
from tradex_trading.execution.position_accountant import PositionAccountant
from tradex_trading.execution.position_manager import PositionManager
from tradex_trading.execution.slippage import (
    FixedSlippageModel,
    NoSlippageModel,
    PercentageSlippageModel,
    SlippageAwareFillSource,
    SlippageModel,
)
from tradex_trading.execution.trading_cache import TradingCache

__all__ = [
    "BrokerFillSource",
    "ExecutionEngine",
    "FeeCalculator",
    "FillSource",
    "FixedSlippageModel",
    "NoSlippageModel",
    "OrderManager",
    "PaperFillSource",
    "PercentageSlippageModel",
    "PositionAccountant",
    "PositionManager",
    "PricingService",
    "ReplayFillSource",
    "RiskManager",
    "SimulatedFillSource",
    "SlippageAwareFillSource",
    "SlippageModel",
    "TradingCache",
]
