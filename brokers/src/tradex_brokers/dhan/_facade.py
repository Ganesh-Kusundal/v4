"""Facade contract for the Dhan REST client mixins.

The ``OrdersMixin``/``PortfolioMixin``/``MarketDataMixin``/``AlertsMixin``/
``AdminMixin`` classes are mixed into :class:`~tradex_brokers.dhan.client.DhanApiClient`,
which owns the shared instance state (``_http``, ``_registry``, …) and the
internal helpers (``_request``, ``_order_payload``, …).  This Protocol is a
type-checking-only contract so mypy can resolve ``self.<member>`` inside the
mixins without importing the concrete client (which would be circular).
"""

from __future__ import annotations

from collections.abc import Callable, Mapping, Sequence
from typing import Any, Protocol

from tradex_domain.enums import OrderType, ProductType
from tradex_domain.execution import Account, Order, OrderRequest, Position
from tradex_domain.instruments import Instrument
from tradex_domain.market import Quote
from tradex_domain.value_objects import InstrumentId, OrderId
from tradex_domain.wire import InstrumentRegistry

from tradex_brokers.common.provider_client import ProviderHttpClient
from tradex_brokers.dhan._admin import AdminMixin
from tradex_brokers.dhan._alerts import AlertsMixin
from tradex_brokers.dhan._marketdata import MarketDataMixin
from tradex_brokers.dhan._orders import OrdersMixin
from tradex_brokers.dhan._portfolio import PortfolioMixin

__all__ = ["DhanClientFacade"]


class DhanClientFacade(
    OrdersMixin,
    PortfolioMixin,
    MarketDataMixin,
    AlertsMixin,
    AdminMixin,
    Protocol,
):
    """Type-checking facade exposing the Dhan client's shared members."""

    _http: ProviderHttpClient
    _registry: InstrumentRegistry
    _client_id: str
    _ws_token_provider: Callable[[], str] | None

    def _security_id(self, instrument_id: InstrumentId) -> str: ...
    def _validated(self, body: object) -> object: ...
    def _url(self, path: str) -> str: ...
    def _request(
        self,
        method: str,
        path: str,
        *,
        cache_read: bool = False,
        **kwargs: Any) -> object: ...
    def _invalidate_after_write(self) -> None: ...
    def _segment(self, instrument: Instrument) -> str: ...
    def _native_product_type(self, product_type: ProductType) -> str: ...
    def _native_order_type(self, request: OrderRequest) -> str: ...
    def _order_payload(self, request: OrderRequest) -> dict[str, object]: ...
    def _has_order_identity(self, row: Mapping[str, Any]) -> bool: ...
    def _domain_order_type(self, value: str, *, has_price: bool = False) -> OrderType: ...
    def _response_order_id(self, body: object, provider: str) -> OrderId: ...
    def _option_exchange(self, underlying: Instrument) -> str: ...
    def _history_instrument_type(self, instrument: Instrument) -> str: ...
    def _batch_segment_map(
        self,
        instruments: Sequence[Instrument],
    ) -> tuple[dict[str, list[int]], dict[str, Instrument]]: ...
    def _order_from_row(
        self,
        row: Mapping[str, Any],
        fallback_id: OrderId | None = None) -> Order: ...
    def _quote_from_row(self, instrument: Instrument, row: dict[str, Any]) -> Quote: ...
    def _positions(self, rows: object) -> list[Position]: ...
    def _account_for_portfolio(self) -> Account: ...
    def get_orderbook(self) -> list[Order]: ...
