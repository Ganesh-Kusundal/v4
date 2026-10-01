"""Shared broker mixins and the provider facade protocol (REF-8).

``ProviderFacade`` declares — exactly once — the helper members that the Dhan
and Upstox facade Protocols previously duplicated; both facades inherit it.
Members whose signatures differ per provider (``_url`` / ``_request`` host
routing, Dhan segment/security-id helpers, Upstox batch-key resolution)
deliberately stay on the per-provider facades.

The shared mixins in this package implement only the methods whose bodies are
byte-identical across providers; anything whose wire format differs stays
per-broker (see ``orders.py`` / ``marketdata.py`` decision records).

Import discipline: this package may import only ``tradex_domain`` and
``tradex_brokers.common`` internals — never a concrete provider module.
"""

from __future__ import annotations

from collections.abc import Mapping
from typing import Any, Protocol

from tradex_domain.enums import ProductType
from tradex_domain.execution import Account, Order, OrderRequest, Position
from tradex_domain.instruments import Instrument
from tradex_domain.market import Quote
from tradex_domain.value_objects import OrderId
from tradex_domain.wire import InstrumentRegistry

from tradex_brokers.common.mixins.admin import SharedAdminMixin
from tradex_brokers.common.mixins.portfolio import SharedPortfolioMixin
from tradex_brokers.common.provider_client import ProviderHttpClient

__all__ = [
    "ProviderFacade",
    "SharedAdminMixin",
    "SharedPortfolioMixin",
]


class ProviderFacade(Protocol):
    """Helper contract shared by every provider facade.

    ``DhanClientFacade`` (``tradex_brokers.dhan._facade``) and ``UptoxFacade``
    (``tradex_brokers.upstox._facade``) both declare these members; they are
    declared here once and the two facade Protocols inherit from this class.
    The shared mixins type their ``self`` against this protocol.
    """

    _http: ProviderHttpClient
    _registry: InstrumentRegistry

    # Public surface the shared mixins compose from.
    def get_account(self) -> Account: ...
    def get_positions(self) -> list[Position]: ...

    # Internal helpers shared verbatim by both provider facades.
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
    def _account_for_portfolio(self) -> Account: ...
