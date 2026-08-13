"""Facade contract for the Upstox REST client mixins.

The ``OrdersMixin``/``PortfolioMixin``/``MarketDataMixin``/``AdminMixin``
classes are mixed into :class:`~tradex_brokers.upstox.client.UpstoxApiClient`,
which owns the shared instance state (``_http``, ``_registry``, …) and the
internal helpers (``_request``, ``_order_payload``, …).  This Protocol is a
type-checking-only contract so mypy can resolve ``self.<member>`` inside the
mixins without importing the concrete client (which would be circular).
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from typing import Any, Protocol

from tradex_domain.enums import ProductType
from tradex_domain.execution import Account, Order, OrderRequest, Position
from tradex_domain.instruments import Instrument
from tradex_domain.market import Quote
from tradex_domain.value_objects import OrderId
from tradex_domain.wire import InstrumentRegistry

from tradex_brokers.common.provider_client import ProviderHttpClient
from tradex_brokers.upstox._admin import AdminMixin
from tradex_brokers.upstox._alerts import AlertsMixin
from tradex_brokers.upstox._marketdata import MarketDataMixin
from tradex_brokers.upstox._orders import OrdersMixin
from tradex_brokers.upstox._portfolio import PortfolioMixin

__all__ = ["UptoxFacade"]


class UptoxFacade(
    OrdersMixin,
    PortfolioMixin,
    MarketDataMixin,
    AlertsMixin,
    AdminMixin,
    Protocol,
):
    """Type-checking facade exposing the Upstox client's shared members."""

    _http: ProviderHttpClient
    _registry: InstrumentRegistry
    _access_token: str

    def _url(self, path: str, *, host: str = "v2") -> str: ...
    def _request(
        self,
        method: str,
        path: str,
        *,
        host: str = "v2",
        cache_read: bool = False,
        **kwargs: Any,
    ) -> object: ...
    def _invalidate_after_write(self) -> None: ...
    def _native_product_type(self, product_type: ProductType) -> str: ...
    def _native_order_type(self, request: OrderRequest) -> str: ...
    def _order_payload(self, request: OrderRequest) -> dict[str, object]: ...
    def _has_order_identity(self, row: Mapping[str, Any]) -> bool: ...
    def _response_order_id(self, body: object, provider: str) -> OrderId: ...
    def _order_from_row(
        self, row: Mapping[str, Any], fallback_id: OrderId | None = None
    ) -> Order: ...
    def _quote_from_row(self, instrument: Instrument, row: dict[str, Any]) -> Quote: ...
    def _positions(self, rows: object) -> list[Position]: ...
    def _market_row(self, raw: object, key: str) -> dict[str, Any]: ...
    def _resolve_batch_keys(
        self, instruments: Sequence[Instrument]
    ) -> tuple[list[str], dict[str, Instrument]]: ...
    def _account_for_portfolio(self) -> Account: ...
