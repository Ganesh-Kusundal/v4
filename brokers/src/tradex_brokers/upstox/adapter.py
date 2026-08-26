"""Upstox broker adapter.

Implements the ``BrokerAdapter`` protocol using the
composed :class:`UpstoxApiClient` for HTTP calls and WebSocket streams.

Without a bound transport the adapter is capability-loud: market-data /
order / portfolio calls raise ``BrokerUnavailableError`` while
``capabilities``, ``load_instruments`` and ``search`` stay usable.
"""

from __future__ import annotations

import logging
from collections.abc import Callable, Iterable, Mapping
from datetime import date
from typing import Any

from tradex_domain.capabilities import require_capability
from tradex_domain.enums import OrderSide, ProductType, Timeframe
from tradex_domain.errors import BrokerUnavailableError, CapabilityNotSupportedError
from tradex_domain.instruments import Instrument
from tradex_domain.market import OHLC, HistoricalSeries
from tradex_domain.options import OptionChain
from tradex_domain.value_objects import InstrumentId, Price
from tradex_domain.wire import InstrumentRegistry

from tradex_brokers.common.base import BaseBroker
from tradex_brokers.common.capabilities import upstox_capabilities
from tradex_brokers.common.endpoints import (
    UPSTOX_HFT_BASE_URL,
    UPSTOX_REST_BASE_URL,
    UPSTOX_V3_BASE_URL,
)
from tradex_brokers.common.provider_common import option_chain_from_master
from tradex_brokers.common.token_lifecycle import TokenLifecyclePort
from tradex_brokers.upstox.client import UpstoxApiClient

log = logging.getLogger(__name__)

_UPSTOX_EQUITY_UNIVERSE = ("RELIANCE", "TCS", "INFY", "HDFCBANK")
_UPSTOX_INDEX_KEYS = {
    "NIFTY": "NSE_INDEX|Nifty 50",
    "BANKNIFTY": "NSE_INDEX|Nifty Bank",
    "FINNIFTY": "NSE_INDEX|Nifty Fin Service",
}


class UpstoxBroker(BaseBroker):
    """Upstox broker adapter — implements BrokerAdapter.

    Uses :class:`UpstoxApiClient` for HTTP calls and WebSocket streams.
    Without a bound transport, trading calls raise ``BrokerUnavailableError``.
    Common lifecycle, gating, and pass-throughs are provided by
    :class:`BaseBroker`; only Upstox-specific behavior lives here.
    """

    _fallback_equities = _UPSTOX_EQUITY_UNIVERSE
    _fallback_index_keys = _UPSTOX_INDEX_KEYS

    def __init__(
        self,
        transport: UpstoxApiClient | None = None,
        registry: InstrumentRegistry | None = None,
        allow_order_operations: bool = True,
        instrument_loader: Callable[[], Iterable[Mapping[str, Any]]] | None = None,
    ) -> None:
        super().__init__(
            capabilities=upstox_capabilities(),
            transport=transport,
            registry=registry,
            allow_order_operations=allow_order_operations,
            instrument_loader=instrument_loader,
        )
        self.rate_limiter = (
            getattr(self._transport, "rate_limiter", None)
            if self._transport is not None
            else None
        )

    @classmethod
    def from_fetch(
        cls,
        *,
        fetch: Callable[..., Any],
        registry: InstrumentRegistry | None = None,
        access_token: str = "",
        token_manager: TokenLifecyclePort | None = None,
        base_url: str = UPSTOX_REST_BASE_URL,
        base_hft: str = UPSTOX_HFT_BASE_URL,
        base_v3: str = UPSTOX_V3_BASE_URL,
        allow_order_operations: bool = True,
        instrument_loader: Callable[[], Iterable[Mapping[str, Any]]] | None = None,
        **http_options: Any,
    ) -> UpstoxBroker:
        """Build an Upstox adapter over an injected resilient HTTP fetch."""
        resolved_registry = registry or InstrumentRegistry()
        client = UpstoxApiClient.from_fetch(
            fetch=fetch,
            registry=resolved_registry,
            access_token=access_token,
            token_manager=token_manager,
            base_url=base_url,
            base_hft=base_hft,
            base_v3=base_v3,
            **http_options,
        )
        broker = cls(
            transport=client,
            registry=resolved_registry,
            allow_order_operations=allow_order_operations,
            instrument_loader=instrument_loader,
        )
        broker.set_token_manager(token_manager)
        return broker

    # ------------------------------------------------------------------
    # portfolio (Upstox-specific)
    # ------------------------------------------------------------------

    def convert_position(
        self,
        instrument: Instrument,
        *,
        from_product: ProductType,
        to_product: ProductType,
        quantity: int,
    ) -> dict[str, object]:
        self._require_mutation()
        return self._require().convert_position(
            instrument,
            from_product=from_product,
            to_product=to_product,
            quantity=quantity,
        )

    # ------------------------------------------------------------------
    # market data (Upstox-specific: option chains)
    # ------------------------------------------------------------------

    def get_option_chain(
        self,
        underlying: Instrument,
        expiry: date | str | None = None,
    ) -> OptionChain:
        require_capability(self.capabilities, "supports_option_chain")
        from_master = option_chain_from_master(self._loaded_instruments, underlying, expiry)
        # MCX and other non-NFO/BFO exchanges have no Upstox REST chain
        # endpoint — the loaded instrument master is the source of truth.
        # NFO/BFO (and IDX-index underlyings) keep the live REST chain
        # (OI/volume/greeks); the master is the fallback.
        exchange = underlying.exchange.value
        if exchange not in ("NFO", "BFO", "IDX") and from_master.expiries():
            return from_master
        if self._transport is None or not self._connected:
            if from_master.expiries():
                return from_master
            raise BrokerUnavailableError("upstox broker not connected")
        if expiry is None:
            if from_master.expiries():
                return from_master
            raise CapabilityNotSupportedError(
                "Upstox option-chain requests require an explicit expiry"
            )
        try:
            return self._require().get_option_chain(underlying, expiry)
        except Exception as exc:  # noqa: BLE001 — fall back, never mask silently
            if from_master.expiries():
                log.warning(
                    "upstox REST option chain failed (%s); serving master chain", exc
                )
                return from_master
            raise

    # ------------------------------------------------------------------
    # instruments
    # ------------------------------------------------------------------

    def _extra_row_meta(self, row: Mapping[str, Any], meta: dict[str, object]) -> None:
        """Upstox contract metadata (position sizing)."""
        lot_size = row.get("lot_size")
        if lot_size not in (None, ""):
            meta["lot_size"] = str(lot_size).strip()

    def subscribe_quotes(self, instruments: object, handler: object) -> object:
        """Subscribe to quote stream via WebSocket backend."""
        if self._ws_backend is not None:
            return self._ws_backend.subscribe_quotes(instruments, handler)
        return None

    def subscribe_depth(self, instrument: object, handler: object) -> object:
        """Subscribe to 5-level depth via the market-data stream backend."""
        if self._ws_backend is not None:
            return self._ws_backend.subscribe_depth(instrument, handler)
        return None

    def subscribe_depth_30(self, instrument: object, handler: object) -> object:
        """Subscribe to 30-level depth via the market-data stream backend."""
        if self._ws_backend is not None:
            return self._ws_backend.subscribe_depth_30(instrument, handler)
        return None

    def unsubscribe(self, subscription: object) -> None:
        """Unsubscribe from a stream."""
        if self._ws_backend is not None:
            self._ws_backend.unsubscribe(subscription)

    def unsubscribe_instruments(self, instruments: object) -> None:
        """Drop *instruments* from the live wire set of any stream backend."""
        if self._ws_backend is None:
            return
        drop = getattr(self._ws_backend, "unsubscribe_instruments", None)
        if callable(drop):
            drop(instruments)

    @property
    def registry(self) -> InstrumentRegistry:
        return self._registry

    # -- auxiliary account surface ---------------------------------------

    def margin(
        self,
        instrument: Instrument,
        *,
        side: OrderSide,
        quantity: int,
        product_type: ProductType = ProductType.INTRADAY,
        price: Price | None = None,
    ) -> dict[str, object]:
        return self._require().margin(
            instrument,
            side=side,
            quantity=quantity,
            product_type=product_type,
            price=price,
        )

    def get_trade_book(self) -> list[dict[str, object]]:
        return list(self._require().get_trade_book())

    def get_news(
        self,
        category: str,
        instrument_keys: list[str] | None = None,
        page_number: int | None = None,
        page_size: int | None = None,
    ) -> list[dict[str, object]]:
        require_capability(self.capabilities, "supports_news")
        return list(
            self._require().get_news(
                category,
                instrument_keys=instrument_keys,
                page_number=page_number,
                page_size=page_size,
            )
        )

    def get_ohlc(
        self, instruments: Iterable[Instrument], interval: str = "1d"
    ) -> dict[InstrumentId, OHLC]:
        return self._require().get_ohlc(list(instruments), interval=interval)

    def intraday_candles(
        self, instrument: Instrument, timeframe: Timeframe | str
    ) -> HistoricalSeries:
        return self._require().intraday_candles(instrument, timeframe)

    # -- streaming -------------------------------------------------------

    def stream_backend(self, *, ws_factory: Any | None = None) -> Any:
        """Order/position stream backend (cached; shared across subscribers)."""
        if self._transport is None:
            raise BrokerUnavailableError("upstox transport not bound")
        if self._order_backend is None:
            with self._stream_lock:
                if self._order_backend is None:  # double-checked lazy init
                    self._order_backend = self._transport.portfolio_stream_backend(
                        ws_factory=ws_factory
                    )
        return self._order_backend

    def market_stream_backend(self, *, ws_factory: Any | None = None) -> Any:
        """Market-data stream backend."""
        if self._transport is None:
            raise BrokerUnavailableError("upstox transport not bound")
        return self._transport.market_stream_backend(ws_factory=ws_factory)

    def depth_stream_backend(
        self, *, total_slots: int = 20, ws_factory: Any | None = None
    ) -> Any:
        """Depth stream backend (delegates to market-data stream).

        Upstox serves depth on the market-data stream itself; the effective
        level count is fixed at subscribe time (``subscribe_depth_30`` /
        ``subscribe_depth``), so ``total_slots`` is accepted for protocol
        parity but not applied here.
        """
        if self._transport is None:
            raise BrokerUnavailableError("upstox transport not bound")
        return self._transport.market_stream_backend(ws_factory=ws_factory)


__all__ = ["UpstoxBroker"]
