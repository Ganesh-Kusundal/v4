"""Facade contract for the Upstox REST client mixins.

The ``OrdersMixin``/``PortfolioMixin``/``MarketDataMixin``/``AdminMixin``
classes are mixed into :class:`~tradex_brokers.upstox.client.UpstoxApiClient`,
which owns the shared instance state (``_http``, ``_registry``, …) and the
internal helpers (``_request``, ``_order_payload``, …).  This Protocol is a
type-checking-only contract so mypy can resolve ``self.<member>`` inside the
mixins without importing the concrete client (which would be circular).

Members shared with every provider facade are declared once on
:class:`~tradex_brokers.common.mixins.ProviderFacade` (REF-8); only
Upstox-specific helpers (v2/HFT/v3 host routing, market-row and batch-key
resolution) remain here.
"""

from __future__ import annotations

from collections.abc import Sequence
from typing import Any, Protocol

from tradex_domain.instruments import Instrument

from tradex_brokers.common.mixins import ProviderFacade
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
    ProviderFacade,
    Protocol,
):
    """Type-checking facade exposing the Upstox client's shared members."""

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
    def _market_row(self, raw: object, key: str) -> dict[str, Any]: ...
    def _resolve_batch_keys(
        self, instruments: Sequence[Instrument]
    ) -> tuple[list[str], dict[str, Instrument]]: ...
