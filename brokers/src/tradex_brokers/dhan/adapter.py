"""Dhan broker adapter.

Implements ``BrokerAdapter`` + ``ExtensionAdapter`` protocols using the
composed :class:`DhanApiClient` for HTTP calls and WebSocket streams.

Without a bound transport the adapter is capability-loud: market-data /
order / portfolio calls raise ``BrokerUnavailableError`` while
``capabilities``, ``load_instruments`` and ``search`` stay usable.
"""

from __future__ import annotations

import logging
from collections.abc import Callable, Iterable, Mapping
from datetime import date
from typing import Any

from tradex_domain.capabilities import dhan_capabilities, require_capability
from tradex_domain.enums import OrderSide, OrderType, ProductType
from tradex_domain.errors import BrokerUnavailableError
from tradex_domain.execution import Order
from tradex_domain.instruments import Future, Instrument
from tradex_domain.options import OptionChain
from tradex_domain.value_objects import InstrumentId, Price
from tradex_domain.wire import InstrumentRegistry

from tradex_brokers.common.base import BaseBroker
from tradex_brokers.common.provider_common import (
    future_chain_from_master,
    option_chain_from_master,
)
from tradex_brokers.common.token_lifecycle import TokenLifecyclePort
from tradex_brokers.dhan.client import DhanApiClient

log = logging.getLogger(__name__)

_DHAN_EQUITY_UNIVERSE = ("RELIANCE", "TCS", "INFY", "HDFCBANK")
_DHAN_INDEX_KEYS = {"NIFTY": "26000", "BANKNIFTY": "26009", "FINNIFTY": "26037"}


class DhanBroker(BaseBroker):
    """Dhan broker adapter — implements BrokerAdapter + ExtensionAdapter.

    Uses :class:`DhanApiClient` for HTTP calls and WebSocket streams.
    Without a bound transport, trading calls raise ``BrokerUnavailableError``.
    Common lifecycle, gating, and pass-throughs are provided by
    :class:`BaseBroker`; only Dhan-specific behavior lives here.
    """

    _fallback_equities = _DHAN_EQUITY_UNIVERSE
    _fallback_index_keys = _DHAN_INDEX_KEYS

    def __init__(
        self,
        transport: DhanApiClient | None = None,
        registry: InstrumentRegistry | None = None,
        allow_order_operations: bool = True,
        instrument_loader: Callable[[], Iterable[Mapping[str, Any]]] | None = None,
    ) -> None:
        super().__init__(
            capabilities=dhan_capabilities(),
            transport=transport,
            registry=registry,
            allow_order_operations=allow_order_operations,
            instrument_loader=instrument_loader,
        )

    @classmethod
    def from_fetch(
        cls,
        *,
        fetch: Callable[..., Any],
        registry: InstrumentRegistry | None = None,
        client_id: str = "",
        access_token: str | None = None,
        token_manager: TokenLifecyclePort | None = None,
        base_url: str = "https://api.dhan.co/v2",
        allow_order_operations: bool = True,
        instrument_loader: Callable[[], Iterable[Mapping[str, Any]]] | None = None,
        **http_options: Any,
    ) -> DhanBroker:
        """Build a Dhan adapter over an injected resilient HTTP fetch."""
        resolved_registry = registry or InstrumentRegistry()
        client = DhanApiClient.from_fetch(
            fetch=fetch,
            registry=resolved_registry,
            client_id=client_id,
            access_token=access_token,
            token_manager=token_manager,
            base_url=base_url,
            **http_options,
        )
        broker = cls(
            transport=client,
            registry=resolved_registry,
            allow_order_operations=allow_order_operations,
            instrument_loader=instrument_loader,
        )
        broker._token_manager = token_manager
        return broker

    # ------------------------------------------------------------------
    # orders (Dhan-specific: order_list)
    # ------------------------------------------------------------------

    def get_order_list(
        self,
        *,
        status: str = "ALL",
        from_date: str | None = None,
        to_date: str | None = None,
        sector: str | None = None,
    ) -> list[Order]:
        return list(
            self._require().get_order_list(
                status=status, from_date=from_date, to_date=to_date, sector=sector
            )
        )

    # ------------------------------------------------------------------
    # portfolio (Dhan-specific)
    # ------------------------------------------------------------------

    def convert_position(
        self,
        instrument: Instrument,
        *,
        from_product: ProductType,
        to_product: ProductType,
        quantity: int,
        position_type: str = "LONG",
    ) -> dict[str, object]:
        self._require_mutation()
        return self._require().convert_position(
            instrument,
            from_product=from_product,
            to_product=to_product,
            quantity=quantity,
            position_type=position_type,
        )

    def mass_status(self) -> dict[str, object]:
        return self._require().mass_status()

    # ------------------------------------------------------------------
    # market data (Dhan-specific: option chains)
    # ------------------------------------------------------------------

    def get_option_chain(
        self,
        underlying: Instrument,
        expiry: date | str | None = None,
    ) -> OptionChain:
        require_capability(self.capabilities, "supports_option_chain")
        if self._transport is None or not self._connected:
            from_master = option_chain_from_master(
                self._loaded_instruments, underlying, expiry
            )
            if from_master.expiries():
                return from_master
            raise BrokerUnavailableError("dhan broker not connected")
        # Dhan serves option chains over REST for NFO/BFO/IDX and for the
        # commodity/currency segments (MCX, NSE_COMM, CDS, BCD) — the API takes
        # the exchange segment (``UnderlyingSeg``) plus a scrip id. Commodity
        # underlyings have no root instrument in the master, so the near-month
        # futures contract is the ``UnderlyingScrip`` (Tradehull parity) unless
        # the caller already passed a specific contract. The master-derived
        # chain is only the REST-failure fallback.
        rest_underlying = underlying
        if underlying.exchange.value in ("MCX", "NSE_COMM") and not isinstance(
            underlying, Future
        ):
            futures = future_chain_from_master(self._loaded_instruments, underlying)
            if futures:
                rest_underlying = futures[0]
        try:
            return self._require().get_option_chain(rest_underlying, expiry)
        except Exception as exc:  # noqa: BLE001 — fall back, never mask silently
            # REST chain unavailable (e.g. unsupported expiry) — serve the
            # master-derived chain when the master carries the strikes.
            from_master = option_chain_from_master(
                self._loaded_instruments, underlying, expiry
            )
            if from_master.expiries():
                log.warning(
                    "dhan REST option chain failed (%s); serving master chain", exc
                )
                return from_master
            raise

    # ------------------------------------------------------------------
    # instruments
    # ------------------------------------------------------------------

    def _extra_row_meta(self, row: Mapping[str, Any], meta: dict[str, object]) -> None:
        """Dhan contract metadata (position sizing / tick maths / slippage)."""
        for name in ("lot_size", "tick_size"):
            value = row.get(name)
            if value not in (None, ""):
                meta[name] = str(value).strip()

    def _extra_row_aliases(
        self,
        fresh: InstrumentRegistry,
        row: Mapping[str, Any],
        iid: InstrumentId,
        key: str,
    ) -> None:
        """Bare security-id alias: Dhan WebSocket frames (market feed and
        depth) and REST responses identify instruments by the numeric id
        alone, while master rows register ``{exchange}:{id}`` keys.
        """
        security_id = str(row.get("security_id", "") or "").strip()
        if security_id and security_id != key:
            fresh.add_alias(security_id, iid)

    def subscribe_quotes(self, instruments: object, handler: object) -> object:
        """Subscribe to quote stream via WebSocket backend."""
        if self._ws_backend is not None:
            return self._ws_backend.subscribe_quotes(instruments, handler)
        return None

    def subscribe_depth(self, instrument: object, handler: object) -> object:
        """Subscribe to depth stream via the dedicated depth-20 WebSocket backend.

        The market-data backend (``_ws_backend``) has no depth endpoint; the
        first call lazily creates ``DhanDepthStreamBackend`` (RequestCode 23
        socket) and reuses it for subsequent subscriptions.
        """
        if self._depth_backend is None:
            if self._transport is None:
                return None
            self._depth_backend = self.depth_stream_backend()
        return self._depth_backend.subscribe_depth(instrument, handler)

    def unsubscribe(self, subscription: object) -> None:
        """Unsubscribe from a stream."""
        if self._ws_backend is not None:
            self._ws_backend.unsubscribe(subscription)
        if self._depth_backend is not None:
            self._depth_backend.unsubscribe(subscription)

    def unsubscribe_instruments(self, instruments: object) -> None:
        """Drop *instruments* from the live wire set of any stream backend."""
        for backend in (self._ws_backend, self._depth_backend):
            if backend is None:
                continue
            drop = getattr(backend, "unsubscribe_instruments", None)
            if callable(drop):
                drop(instruments)

    @property
    def registry(self) -> InstrumentRegistry:
        return self._registry

    # ------------------------------------------------------------------
    # ExtensionAdapter — Dhan-specific super / forever / eDIS lifecycle
    # ------------------------------------------------------------------

    def generate_tpin(self) -> dict[str, object]:
        require_capability(self.capabilities, "supports_edis")
        return self._require().edis_status("")

    def edis_status(self, isin: str) -> dict[str, object]:
        require_capability(self.capabilities, "supports_edis")
        return self._require().edis_status(isin)

    def authorize_edis(
        self, isin: str, quantity: int, exchange: str
    ) -> dict[str, object]:
        self._require_mutation()
        return self._require().authorize_edis(isin, quantity, exchange)

    # -- auxiliary account surface ----------------------------------------

    def margin_calculator(
        self,
        instrument: Instrument,
        *,
        side: OrderSide,
        quantity: int,
        product_type: ProductType = ProductType.INTRADAY,
        price: Price | None = None,
        order_type: OrderType | None = None,
    ) -> dict[str, object]:
        return self._require().margin_calculator(
            instrument,
            side=side,
            quantity=quantity,
            product_type=product_type,
            price=price,
            order_type=order_type,
        )

    def get_trade_history(
        self,
        instrument_id: str | None = None,
        from_date: str | None = None,
        to_date: str | None = None,
    ) -> list[dict[str, object]]:
        return list(
            self._require().get_trade_history(
                instrument_id=instrument_id, from_date=from_date, to_date=to_date
            )
        )

    def trade_book(self) -> list[dict[str, object]]:
        return list(self._require().trade_book())

    # -- streaming --------------------------------------------------------

    def stream_backend(self, *, ws_factory: Any | None = None) -> Any:
        """Order-update stream backend (cached; shared across subscribers)."""
        if self._transport is None:
            raise BrokerUnavailableError("dhan transport not bound")
        if self._order_backend is None:
            self._order_backend = self._transport.order_stream_backend(ws_factory=ws_factory)
        return self._order_backend

    def market_stream_backend(self, *, ws_factory: Any | None = None) -> Any:
        """Quote tick stream backend."""
        if self._transport is None:
            raise BrokerUnavailableError("dhan transport not bound")
        return self._transport.market_stream_backend(ws_factory=ws_factory)

    def depth_stream_backend(
        self, *, total_slots: int = 20, ws_factory: Any | None = None
    ) -> Any:
        """Depth stream backend."""
        if self._transport is None:
            raise BrokerUnavailableError("dhan transport not bound")
        return self._transport.depth_stream_backend(
            total_slots=total_slots, ws_factory=ws_factory
        )


__all__ = ["DhanBroker"]
