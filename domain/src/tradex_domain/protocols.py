"""Broker adapter contracts (D-16).

``BrokerAdapter`` is the full adapter surface a broker plugin must satisfy;
broker-specific features (super/forever/slice orders, eDIS) are part of the
same surface and are gated at runtime by ``BrokerCapabilities`` flags rather
than by separate protocols.
"""

from __future__ import annotations

from collections.abc import Callable, Iterator, Sequence
from datetime import UTC, date, datetime
from decimal import Decimal
from typing import Protocol, runtime_checkable

from tradex_domain.capabilities import BrokerCapabilities
from tradex_domain.enums import BrokerId, ProductType, Timeframe
from tradex_domain.events import DomainEvent
from tradex_domain.execution import (
    Account,
    Fill,
    Order,
    OrderReceipt,
    OrderRequest,
    PortfolioSnapshot,
    Position,
)
from tradex_domain.instruments import Instrument
from tradex_domain.market import Depth, HistoricalSeries, Quote
from tradex_domain.options import OptionChain
from tradex_domain.value_objects import InstrumentId, OrderId, Price


@runtime_checkable
class TradingCacheProtocol(Protocol):
    """In-memory OMS cache contract (orders, positions, latest quotes).

    Shared by the paper broker's internal cache and the trading execution
    caches so both layers present a single storage interface. Implementations
    may expose extra ``set_*`` convenience aliases beyond this minimum.
    """

    # orders
    def update_order(self, order: Order) -> None: ...
    def get_order(self, order_id: OrderId | str) -> Order | None: ...
    def all_orders(self) -> list[Order]: ...

    # positions
    def update_position(self, position: Position) -> None: ...
    def get_position(
        self, instrument: Instrument | InstrumentId | str
    ) -> Position | None: ...
    def all_positions(self) -> list[Position]: ...

    # quotes
    def update_quote(self, quote: Quote) -> None: ...
    def get_quote(self, instrument: Instrument | InstrumentId | str) -> Quote | None: ...

    # snapshots / lifecycle
    def snapshot(self) -> dict[str, dict]: ...
    def restore(self, data: dict[str, dict]) -> None: ...
    def clear(self) -> None: ...


@runtime_checkable
class BrokerAdapter(Protocol):
    """Full adapter surface: lifecycle, orders, portfolio, market data, instruments."""

    capabilities: BrokerCapabilities

    def connect(self) -> None: ...
    def close(self) -> None: ...

    # orders
    def submit_order(self, request: OrderRequest) -> OrderId: ...
    def cancel_order(self, order_id: OrderId) -> Order: ...
    def modify_order(self, order_id: OrderId, request: OrderRequest) -> Order: ...
    def get_order(self, order_id: OrderId) -> Order: ...
    def get_orderbook(self) -> list[Order]: ...

    # super (bracket) orders — same surface, gated at runtime by the
    # ``supports_super_order`` capability flag (see module docstring). The
    # adapters raise ``CapabilityNotSupportedError`` when the flag is off.
    def submit_super_order(self, request: OrderRequest) -> OrderId: ...
    def modify_super_order(
        self, order_id: OrderId, request: OrderRequest
    ) -> object: ...
    def cancel_super_order(self, order_id: OrderId) -> object: ...

    # portfolio
    def get_positions(self) -> list[Position]: ...
    def get_holdings(self) -> list[Position]: ...
    def get_account(self) -> Account: ...
    def get_portfolio(self) -> PortfolioSnapshot: ...

    def convert_position(
        self,
        instrument: Instrument,
        *,
        from_product: ProductType,
        to_product: ProductType,
        quantity: int,
    ) -> dict[str, object]: ...

    # OMS projection seam: whether the adapter already applies fills to its
    # own book, and the cache that projection lives in (None when it keeps
    # none). The execution engine reads both instead of ``getattr`` probing.
    @property
    def owns_position_projection(self) -> bool: ...

    @property
    def trading_cache(self) -> TradingCacheProtocol | None: ...

    # market data
    def get_quote(self, instrument: Instrument) -> Quote: ...
    def ltp(self, instrument: Instrument) -> Price: ...
    def depth(self, instrument: Instrument) -> Depth: ...
    def history(
        self,
        instrument: Instrument,
        timeframe: Timeframe,
        start: datetime,
        end: datetime,
    ) -> HistoricalSeries: ...
    def get_option_chain(
        self,
        underlying: Instrument,
        expiry: date | str | None = None,
    ) -> OptionChain: ...
    def search(self, query: str) -> list[Instrument]: ...

    # streaming
    def stream_backend(self, *, ws_factory: object | None = None) -> object:
        """Order/portfolio update stream backend (no socket until subscribe)."""
        ...

    def market_stream_backend(self, *, ws_factory: object | None = None) -> object:
        """Quote-tick market-data stream backend (no socket until subscribe)."""
        ...

    def depth_stream_backend(
        self,
        *,
        total_slots: int = 20,
        ws_factory: object | None = None,
    ) -> object:
        """Depth stream backend (no socket until subscribe)."""
        ...

    def subscribe_quotes(
        self,
        instruments: Sequence[Instrument],
        handler: Callable[[Quote], None],
    ) -> object:
        """Subscribe to quote stream for given instruments."""
        ...

    def subscribe_depth(
        self,
        instrument: Instrument,
        handler: Callable[[Depth], None],
    ) -> object:
        """Subscribe to depth stream for given instrument."""
        ...

    def subscribe_depth_30(
        self,
        instruments: Sequence[Instrument],
        handler: Callable[[Depth], None],
    ) -> object:
        """Subscribe to the 30-level depth stream (Upstox-only capability).

        Present on the single adapter surface but gated at runtime by
        ``BrokerCapabilities.depth_levels`` (>= 30) — callers must check the
        table rather than probe for the method. Adapters without the stream
        return an inert subscription (None).
        """
        ...

    def unsubscribe(self, subscription: object) -> None:
        """Unsubscribe from a stream."""
        ...

    def unsubscribe_instruments(self, instruments: Sequence[Instrument]) -> None:
        """Drop *instruments* from the backend wire set (best-effort).

        Keeps reconnects from resurrecting unsubscribed instruments; adapters
        whose backends carry no server-side wire set are no-ops.
        """
        ...

    def set_reconnect_hook(self, cb: Callable[[], None] | None) -> None:
        """Register a callback fired after every successful WS reconnect.

        The trading layer wires its feed-resync hook here instead of duck-typed
        attribute probing. Pass ``None`` to clear. Adapters without live
        sockets accept and ignore the hook.
        """
        ...

    # instruments
    def load_instruments(self) -> None: ...


@runtime_checkable
class SessionFacade(Protocol):
    """Minimal session surface a strategy needs (D-12).

    Declares the properties a strategy can access on a trading session.
    Service properties use ``object`` because domain cannot import trading
    types — the goal is IDE autocomplete hints, not strict typing.
    Kept in sync with ``TradingSession``'s actual service surface:
    trade / portfolio / stream / scanner.
    """

    @property
    def trade(self) -> object: ...

    @property
    def portfolio(self) -> object: ...

    @property
    def stream(self) -> object: ...

    @property
    def scanner(self) -> object: ...

    @property
    def state(self) -> object: ...

    @property
    def broker_id(self) -> BrokerId: ...

    @property
    def mode(self) -> str: ...

    @property
    def bus(self) -> object: ...


@runtime_checkable
class Clock(Protocol):
    """Deterministic clock abstraction.

    Allows backtest and live engines to share the same time API.
    Satisfied structurally by any object with a ``now() -> datetime`` method.
    """

    def now(self) -> datetime: ...


class SystemClock:
    """Wall-clock ``Clock`` for live/paper sessions.

    Backtests inject ``FakeClock`` (or pass ``now=``) instead — RiskManager
    refuses to evaluate without an explicit clock or instant.
    """

    def now(self) -> datetime:
        return datetime.now(UTC)


@runtime_checkable
class IndicatorComputer(Protocol):
    """Indicator computation contract.

    Satisfied structurally by any object with an
    ``indicator(series, name, **params) -> HistoricalSeries`` method.
    Lets ScannerEngine depend on a protocol instead of a concrete AnalyticsEngine.
    """

    def indicator(self, series: object, name: str, **params: object) -> object: ...


@runtime_checkable
class BrokerClientPort(Protocol):
    """Minimal shared surface for provider REST facades (SMELL-11).

    The three client facades (Dhan, Upstox, Paper via adapter) were coupled
    only by a naming convention (``_http``, ``_registry``, ...).  This
    Protocol makes that contract explicit and checkable with ``isinstance``,
    so a rename in one facade is caught by type-checking rather than at
    runtime.  Kept intentionally thin — ponytail: the laziest interface that
    eliminates the shotgun rename.

    Note: adapter-level pass-throughs live on ``BaseBroker._transport``
    (a different layer); this port is the client-facade layer where the
    mixins actually bind.
    """

    _registry: object
    _http: object


@runtime_checkable
class MarketStreamPort(Protocol):
    """Live quote-tick subscription surface (SMELL-05 [REF-4]).

    Satisfied by the Dhan/Upstox WS market backends and ``NullStreamBackend``;
    consumed by ``BaseBroker``/adapters instead of duck-typed attribute pokes.
    """

    def subscribe_quotes(self, instruments: object, handler: object) -> object: ...
    def unsubscribe(self, subscription: object) -> None: ...
    def close(self) -> None: ...


@runtime_checkable
class DepthStreamPort(Protocol):
    """Live depth subscription surface (30-level or 5-level)."""

    def subscribe_depth(self, instrument: object, handler: object) -> object: ...
    def unsubscribe(self, subscription: object) -> None: ...
    def close(self) -> None: ...


@runtime_checkable
class OrderStreamPort(Protocol):
    """Live order/position update subscription surface."""

    def subscribe_orders(self, handler: object) -> object: ...
    def unsubscribe(self, subscription: object) -> None: ...
    def close(self) -> None: ...


@runtime_checkable
class MasterRefreshProvider(Protocol):
    """Broker surface providing the daily instrument-master refresh hook.

    Previously consumed via ``getattr(broker, "master_loader", None)`` +
    a naming convention; now an explicit, isinstance-checkable contract.
    """

    master_loader: object

    def ensure_master_fresh(self, *, force_refresh: bool = False) -> None: ...


@runtime_checkable
class OrderCommandPort(Protocol):
    """The single order-command door, satisfied by ``ExecutionEngine``.

    Declared here, in domain, because it is a *contract* every execution mode
    binds to rather than an implementation detail of any one of them: live,
    paper, backtest and replay all place orders through this one surface, and
    the invariant that matters is that no other surface exists.

    **Why a protocol when ``ExecutionEngine.submit`` already reads as the
    door.** The engine's docstring asserts AC-22 ("submit() is the ONLY way an
    order reaches the venue") and the engine deliberately dropped its old bus
    subscriptions to make that true. But an assertion in a docstring is not a
    boundary — nothing failed when ``BacktestEngine.submit`` grew a second
    ``fill_source.submit`` path that skipped risk, idempotency and the cash
    floor entirely, because that method sidestepped the engine rather than
    contradicting it. This protocol is the named, checkable form of the same
    claim: a caller holding an ``OrderCommandPort`` cannot reach a venue by any
    other route, because there is no other method on it.

    ``submit`` is the only method that creates an order. ``cancel`` is declared
    because it is part of the OMS lifecycle a caller needs, and it mutates an
    order that already exists rather than creating one — so it is not a second
    way in.
    """

    def submit(self, request: OrderRequest) -> OrderReceipt: ...

    def cancel(self, order_id: OrderId) -> Order: ...


@runtime_checkable
class EventStore(Protocol):
    """Append-only domain event stream."""

    def append(self, event: DomainEvent) -> int: ...
    def replay(self, stream: str = "orders") -> Iterator[DomainEvent]: ...


__all__ = [
    "BrokerAdapter",
    "BrokerClientPort",
    "Clock",
    "DepthStreamPort",
    "EventStore",
    "IndicatorComputer",
    "MasterRefreshProvider",
    "MarketStreamPort",
    "OrderCommandPort",
    "OrderStreamPort",
    "SessionFacade",
    "TradingCacheProtocol",
]


# ---------------------------------------------------------------------------
# Zero-Discrepancy Environment Abstractions
# ---------------------------------------------------------------------------

@runtime_checkable
class TradingClock(Clock, Protocol):
    """Extended clock with control methods for simulation.

    LiveClock: real wall-clock, advance() is no-op.
    SimClock: deterministic, advance() moves time forward.
    ReplayClock: feeds pre-recorded event timestamps.
    """

    def advance(self, to: datetime) -> None: ...
    def is_live(self) -> bool: ...


@runtime_checkable
class MarketDataProvider(Protocol):
    """Unified market data access across all environments.

    Every environment returns identical Bar/Quote objects for the same
    (instrument, timeframe, timestamp) tuple.
    """

    def get_bar(
        self, instrument: Instrument, timeframe: Timeframe, ts: datetime
    ) -> object | None: ...

    def get_bars(
        self,
        instrument: Instrument,
        timeframe: Timeframe,
        start: datetime,
        end: datetime,
    ) -> list: ...

    def get_quote(self, instrument: Instrument) -> object: ...

    def get_ltp(self, instrument: Instrument) -> Price: ...


@runtime_checkable
class OrderGateway(Protocol):
    """Unified order execution across all environments.

    LiveGateway: routes to broker.
    PaperGateway: simulates fills, tracks paper portfolio.
    SimGateway: deterministic fill simulation for backtest/replay.
    """

    def submit_order(self, request: object) -> object: ...

    def cancel_order(self, order_id: object) -> object: ...

    def modify_order(self, order_id: object, request: object) -> object: ...

    def get_order(self, order_id: object) -> object: ...

    def get_fills(self, order_id: object) -> list: ...
