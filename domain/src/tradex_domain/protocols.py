"""Broker adapter contracts (D-16).

``BrokerAdapter`` is the full adapter surface a broker plugin must satisfy.
``ExtensionAdapter`` adds the capability-exposed broker-specific features.
"""

from __future__ import annotations

from collections.abc import Callable, Sequence
from datetime import date, datetime, timedelta
from typing import Protocol, runtime_checkable

from tradex_domain.capabilities import BrokerCapabilities
from tradex_domain.enums import BrokerId, Timeframe
from tradex_domain.execution import (
    Account,
    Order,
    OrderRequest,
    OrderResult,
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

    # portfolio
    def get_positions(self) -> list[Position]: ...
    def get_holdings(self) -> list[Position]: ...
    def get_account(self) -> Account: ...
    def get_portfolio(self) -> PortfolioSnapshot: ...

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

    def unsubscribe(self, subscription: object) -> None:
        """Unsubscribe from a stream."""
        ...

    # instruments
    def load_instruments(self) -> None: ...


@runtime_checkable
class ExtensionAdapter(BrokerAdapter, Protocol):
    """Capability-exposed broker extensions (D-16)."""

    def submit_super_order(self, request: OrderRequest) -> OrderId: ...
    def submit_forever_order(self, request: OrderRequest) -> OrderId: ...
    def submit_slice_order(
        self,
        request: OrderRequest,
        slices: int,
        interval: timedelta | None,
    ) -> list[OrderId]: ...
    def submit_edis(self, request: OrderRequest) -> OrderId: ...


@runtime_checkable
class SuperOrderAdapter(Protocol):
    """Broker surface gated by ``supports_super_order`` (D-16).

    Includes the full super-order lifecycle so consumers can bind a broker
    structurally (``isinstance``) instead of ``cast`` before mutating.
    """

    def submit_super_order(self, request: OrderRequest) -> OrderId: ...
    def modify_super_order(
        self, order_id: OrderId, request: OrderRequest
    ) -> OrderResult: ...
    def cancel_super_order(
        self, order_id: OrderId, leg: str = "ENTRY"
    ) -> OrderResult: ...
    def list_super_orders(self) -> list[OrderResult]: ...


@runtime_checkable
class ForeverOrderAdapter(Protocol):
    """Broker surface gated by ``supports_forever_order`` (D-16)."""

    def submit_forever_order(self, request: OrderRequest) -> OrderId: ...
    def modify_forever_order(
        self, order_id: OrderId, request: OrderRequest
    ) -> OrderResult: ...
    def cancel_forever_order(self, order_id: OrderId) -> OrderResult: ...
    def list_forever_orders(self) -> list[OrderResult]: ...


@runtime_checkable
class SliceOrderAdapter(Protocol):
    """Broker surface gated by ``supports_slice_order`` (D-16)."""

    def submit_slice_order(
        self,
        request: OrderRequest,
        slices: int,
        interval: timedelta | None = None,
    ) -> list[OrderId]: ...


@runtime_checkable
class EdisAdapter(Protocol):
    """Broker surface gated by ``supports_edis`` (D-16)."""

    def submit_edis(self, request: OrderRequest) -> OrderId: ...
    def generate_tpin(self) -> dict[str, object]: ...
    def edis_status(self, isin: str) -> dict[str, object]: ...


@runtime_checkable
class KillSwitchAdapter(Protocol):
    """Broker surface gated by ``supports_kill_switch`` (D-16)."""

    def kill_switch(self, enable: bool = True) -> dict[str, object]: ...
    def status_kill_switch(self) -> dict[str, object]: ...


@runtime_checkable
class SessionFacade(Protocol):
    """Minimal session surface a strategy needs (D-12).

    Declares the properties a strategy can access on a trading session.
    Service properties use ``object`` because domain cannot import trading
    types — the goal is IDE autocomplete hints, not strict typing.
    """

    @property
    def market(self) -> object: ...

    @property
    def trade(self) -> object: ...

    @property
    def portfolio(self) -> object: ...

    @property
    def stream(self) -> object: ...

    @property
    def scanner(self) -> object: ...

    @property
    def analytics(self) -> object: ...

    @property
    def extension(self) -> object: ...

    @property
    def state(self) -> object: ...

    @property
    def broker_id(self) -> BrokerId: ...

    @property
    def mode(self) -> str: ...

    @property
    def bus(self) -> object: ...


__all__ = [
    "BrokerAdapter",
    "EdisAdapter",
    "ExtensionAdapter",
    "ForeverOrderAdapter",
    "KillSwitchAdapter",
    "SessionFacade",
    "SliceOrderAdapter",
    "SuperOrderAdapter",
    "TradingCacheProtocol",
]
