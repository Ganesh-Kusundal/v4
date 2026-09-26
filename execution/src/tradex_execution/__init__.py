"""Execution engine and OMS for the TradeX v4 trading platform.

Public intake interface: the engine, fill sources, fee calculation,
order/position management, and the trading cache.  Internal types
(idempotency guards, the sqlite persistence seam, risk internals) live
in their respective submodules — import from there directly.
"""

from tradex_execution.engine import ExecutionEngine, RiskManager
from tradex_execution.fees import FeeCalculator, PricingService
from tradex_execution.fill_sources import (
    BrokerFillSource,
    FillSource,
    PaperFillSource,
    ReplayFillSource,
    SimulatedFillSource,
)
from tradex_execution.order_manager import OrderManager
from tradex_execution.position_accountant import PositionAccountant
from tradex_execution.position_manager import PositionManager
from tradex_execution.projection import execution_projection
from tradex_execution.slippage import (
    FixedSlippageModel,
    NoSlippageModel,
    PercentageSlippageModel,
    SlippageAwareFillSource,
    SlippageModel,
)
from tradex_execution.trading_cache import TradingCache

