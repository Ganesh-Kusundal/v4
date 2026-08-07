"""Execution engine and OMS for the TradeX v4 trading platform.

Provides the reactive execution pipeline, order/position management,
fill sources, fee calculation, reconciliation, and persistent storage.
"""

from tradex_trading.execution.engine import (
    ExecutionEngine,
    IdempotencyDuplicate,
    IdempotencyGuard,
    InMemoryOrderStore,
    MemoryIdempotencyGuard,
    OrderStore,
    RiskCheckResult,
    RiskManager,
)
from tradex_trading.execution.fees import FeeBreakdown, FeeCalculator, PricingService
from tradex_trading.execution.fill_sources import (
    BrokerFillSource,
    FillSource,
    PaperFillSource,
    ReplayFillSource,
    SimulatedFillSource,
)
from tradex_trading.execution.order_manager import OrderManager
from tradex_trading.execution.position_manager import PositionManager
from tradex_trading.execution.reconciliation import (
    DriftItem,
    DriftSeverity,
    ReconciliationEngine,
)
from tradex_trading.execution.slippage import (
    FixedSlippageModel,
    NoSlippageModel,
    PercentageSlippageModel,
    SlippageAwareFillSource,
    SlippageModel,
)
from tradex_trading.execution.sqlite_store import SQLiteIdempotencyGuard, SQLiteOrderStore
from tradex_trading.execution.trading_cache import TradingCache

__all__ = [
    "BrokerFillSource",
    "DriftItem",
    "DriftSeverity",
    "ExecutionEngine",
    "FeeBreakdown",
    "FeeCalculator",
    "FillSource",
    "IdempotencyDuplicate",
    "IdempotencyGuard",
    "InMemoryOrderStore",
    "MemoryIdempotencyGuard",
    "OrderManager",
    "OrderStore",
    "PaperFillSource",
    "PositionManager",
    "PricingService",
    "ReconciliationEngine",
    "ReplayFillSource",
    "RiskCheckResult",
    "RiskManager",
    "SQLiteIdempotencyGuard",
    "SQLiteOrderStore",
    "SimulatedFillSource",
    "FixedSlippageModel",
    "NoSlippageModel",
    "PercentageSlippageModel",
    "SlippageAwareFillSource",
    "SlippageModel",
    "TradingCache",
]
