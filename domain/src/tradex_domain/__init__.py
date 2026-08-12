"""v4 canonical domain package.

Owns entities, value objects, enums, errors, protocols, capabilities,
wire mapping, and domain events. Never imports broker or infrastructure code.
"""

from tradex_domain.capabilities import (
    BrokerCapabilities,
    dhan_capabilities,
    paper_capabilities,
    require_capability,
    upstox_capabilities,
)
from tradex_domain.enums import (
    AssetClass,
    BrokerId,
    ExchangeId,
    InstrumentType,
    OrderSide,
    OrderStatus,
    OrderType,
    ProductType,
    Timeframe,
    TimeInForce,
)
from tradex_domain.errors import (
    AuthenticationError,
    BrokerUnavailableError,
    CapabilityNotSupportedError,
    ConnectionTimeoutError,
    InstrumentNotFoundError,
    OrderRejectedError,
    OrderSubmissionUnknownError,
    RateLimitError,
    SDKError,
    SessionStateError,
)
from tradex_domain.events import (
    CandleReceived,
    DomainEvent,
    ErrorOccurred,
    OrderFilled,
    OrderPlaced,
    OrderRejected,
    PlaceOrderCommand,
)
from tradex_domain.execution import (
    Account,
    Fill,
    Order,
    OrderReceipt,
    OrderRequest,
    PortfolioSnapshot,
    Position,
)
from tradex_domain.instruments import (
    Commodity,
    Currency,
    Equity,
    Future,
    Index,
    Instrument,
    InstrumentMeta,
    Option,
)
from tradex_domain.market import OHLC, Candle, Depth, HistoricalSeries, Quote
from tradex_domain.options import Expiry, OptionChain, OptionPair
from tradex_domain.protocols import (
    BrokerAdapter,
    Clock,
    ExtensionAdapter,
    IndicatorComputer,
    SessionFacade,
)
from tradex_domain.serialization import Serializable, from_dict, to_dict
from tradex_domain.strategy import (
    Condition,
    ScannerDefinition,
    ScannerResult,
    Signal,
    StrategyContext,
)
from tradex_domain.value_objects import (
    AccountId,
    CorrelationId,
    InstrumentId,
    Money,
    OrderId,
    Price,
    Quantity,
)
from tradex_domain.wire import (
    InstrumentRegistry,
    WireAdapter,
    normalize_exchange,
    normalize_symbol,
)

__all__ = [
    # enums
    "AssetClass",
    "BrokerId",
    "ExchangeId",
    "InstrumentType",
    "OrderSide",
    "OrderStatus",
    "OrderType",
    "ProductType",
    "TimeInForce",
    "Timeframe",
    # errors
    "AuthenticationError",
    "BrokerUnavailableError",
    "CapabilityNotSupportedError",
    "ConnectionTimeoutError",
    "InstrumentNotFoundError",
    "OrderRejectedError",
    "OrderSubmissionUnknownError",
    "RateLimitError",
    "SDKError",
    "SessionStateError",
    # instruments
    "Commodity",
    "Currency",
    "Equity",
    "Future",
    "Index",
    "Instrument",
    "InstrumentMeta",
    "Option",
    # market
    "Candle",
    "Depth",
    "HistoricalSeries",
    "OHLC",
    "Quote",
    # options
    "Expiry",
    "OptionChain",
    "OptionPair",
    # execution
    "Account",
    "Fill",
    "Order",
    "OrderReceipt",
    "OrderRequest",
    "PortfolioSnapshot",
    "Position",
    # strategy
    "Condition",
    "ScannerDefinition",
    "ScannerResult",
    "Signal",
    "StrategyContext",
    # value objects
    "AccountId",
    "CorrelationId",
    "InstrumentId",
    "Money",
    "OrderId",
    "Price",
    "Quantity",
    # capabilities + protocols
    "BrokerAdapter",
    "BrokerCapabilities",
    "Clock",
    "ExtensionAdapter",
    "IndicatorComputer",
    "InstrumentRegistry",
    "SessionFacade",
    "WireAdapter",
    "dhan_capabilities",
    "normalize_exchange",
    "normalize_symbol",
    "paper_capabilities",
    "require_capability",
    "upstox_capabilities",
    # serialization
    "Serializable",
    "from_dict",
    "to_dict",
    # events
    "CandleReceived",
    "DomainEvent",
    "ErrorOccurred",
    "OrderFilled",
    "OrderPlaced",
    "OrderRejected",
    "PlaceOrderCommand",
]
