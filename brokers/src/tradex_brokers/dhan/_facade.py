"""Facade contract for the Dhan REST client mixins.

The ``OrdersMixin``/``PortfolioMixin``/``MarketDataMixin``/``AdminMixin``
classes are mixed into :class:`~tradex_brokers.dhan.client.DhanApiClient`,
which owns the shared instance state (``_http``, ``_registry``, …) and the
internal helpers (``_request``, ``_order_payload``, …).  This Protocol is a
type-checking-only contract so mypy can resolve ``self.<member>`` inside the
mixins without importing the concrete client (which would be circular).

Members shared with every provider facade are declared once on
:class:`~tradex_brokers.common.mixins.ProviderFacade` (REF-8); only
Dhan-specific helpers remain here.
"""

from __future__ import annotations

from collections.abc import Callable, Sequence
from typing import Any, Protocol

from tradex_domain.enums import OrderType
from tradex_domain.execution import Order
from tradex_domain.instruments import Instrument
from tradex_domain.value_objects import InstrumentId

from tradex_brokers.common.mixins import ProviderFacade
from tradex_brokers.dhan._admin import AdminMixin
from tradex_brokers.dhan._marketdata import MarketDataMixin
from tradex_brokers.dhan._orders import OrdersMixin
from tradex_brokers.dhan._portfolio import PortfolioMixin

__all__ = ["DhanClientFacade"]


class DhanClientFacade(
    OrdersMixin,
    PortfolioMixin,
    MarketDataMixin,
    AdminMixin,
    ProviderFacade,
    Protocol,
):
    """Type-checking facade exposing the Dhan client's shared members."""

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
    def _segment(self, instrument: Instrument) -> str: ...
    def _domain_order_type(self, value: str, *, has_price: bool = False) -> OrderType: ...
    def _option_exchange(self, underlying: Instrument) -> str: ...
    def _history_instrument_type(self, instrument: Instrument) -> str: ...
    def _batch_segment_map(
        self,
        instruments: Sequence[Instrument],
    ) -> tuple[dict[str, list[int]], dict[str, Instrument]]: ...
    def get_orderbook(self) -> list[Order]: ...
