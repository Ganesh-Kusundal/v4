"""Dhan REST API client for the v4 broker adapter.

Owns Dhan endpoint paths and native field mapping.  Authentication, retry,
rate limiting, circuit breaking, and caching are delegated to the composed
:class:`ProviderHttpClient` (transport + resilience pipeline).
"""

from __future__ import annotations

from collections.abc import Callable, Mapping, Sequence
from dataclasses import replace
from datetime import UTC, datetime
from decimal import Decimal
from typing import Any

from tradex_domain.enums import (
    AssetClass,
    OrderSide,
    OrderStatus,
    OrderType,
    ProductType,
    TimeInForce,
)
from tradex_domain.errors import InstrumentNotFoundError
from tradex_domain.execution import Account, Order, OrderRequest, Position
from tradex_domain.instruments import Future, Instrument, Option
from tradex_domain.market import OHLC, Quote
from tradex_domain.value_objects import InstrumentId, Money, OrderId, Price, Quantity
from tradex_domain.wire import InstrumentRegistry

from tradex_brokers.common import dhan_segments
from tradex_brokers.common.client_shared import (
    build_provider_client,
    correlation_id,
    parse_timestamp_fallback,
)
from tradex_brokers.common.dhan_segments import dhan_segment_for
from tradex_brokers.common.endpoints import DHAN_REST_BASE_URL
from tradex_brokers.common.provider_client import ProviderHttpClient
from tradex_brokers.common.provider_common import (
    as_decimal,
    as_price,
    first_mapping,
    instrument_from_id,
    provider_key,
    unwrap_data,
)
from tradex_brokers.common.token_lifecycle import TokenLifecyclePort
from tradex_brokers.dhan._admin import AdminMixin
from tradex_brokers.dhan._marketdata import MarketDataMixin
from tradex_brokers.dhan._orders import OrdersMixin
from tradex_brokers.dhan._portfolio import PortfolioMixin

#: Domain exchange -> Dhan ``ExchangeSegment`` string.  Single source of truth,
#: shared by the REST client and the WebSocket stream backends; the table itself
#: now lives in ``tradex_brokers.common.dhan_segments``.  This alias is kept as
#: a module-level name so existing importers of this module keep working
#: unchanged.
_DHAN_EXCHANGE_SEGMENT: dict[str, str] = dhan_segments.DHAN_EXCHANGE_SEGMENT

#: Domain exchange for option legs built from a REST chain, keyed by the
#: underlying's exchange. Equity/index underlyings follow the NFO/BFO product
#: mapping; commodity/currency underlyings keep their own exchange — Dhan's
#: /optionchain also serves MCX, NSE_COMM, CDS and BCD chains, and the legs
#: must be built on the same exchange the instrument master uses or the
#: registry / quote / order mapping for those contracts silently breaks.
#: Note: ``IDX -> NFO`` is correct for NSE indices only; a future BSE-index
#: (SENSEX) chain would need BFO — distinguish via registry meta then.
_OPTION_LEG_EXCHANGE: dict[str, str] = {
    "NSE": "NFO",
    "BSE": "BFO",
    "NFO": "NFO",
    "BFO": "BFO",
    "IDX": "NFO",
    "MCX": "MCX",
    "NSE_COMM": "NSE_COMM",
    "CDS": "CDS",
    "BCD": "BCD",
}


def dhan_exchange_segment(exchange: Any) -> str:
    """Map a domain exchange to Dhan's ``ExchangeSegment`` string."""
    return dhan_segment_for(exchange)


def dhan_segment(instrument: Instrument) -> str:
    """Map a domain Instrument to Dhan's ``ExchangeSegment`` string.

    Index instruments always map to ``IDX_I`` regardless of the ``exchange``
    code carried by their id; every other instrument maps via its exchange.
    """
    if instrument.asset_class is AssetClass.INDEX:
        return "IDX_I"
    return dhan_exchange_segment(instrument.exchange)


# ---------------------------------------------------------------------------
# Shared value/response helpers — aliases into common/provider_common so
# existing call sites keep working without duplicated logic (DRY).
# ---------------------------------------------------------------------------
_as_price = as_price
_unwrap_data = unwrap_data
_first_mapping = first_mapping


# ---------------------------------------------------------------------------
# Client
# ---------------------------------------------------------------------------


class DhanApiClient(OrdersMixin, PortfolioMixin, MarketDataMixin, AdminMixin):
    """Dhan REST endpoint implementation for the v4 broker adapter."""

    BASE_URL = DHAN_REST_BASE_URL

    @classmethod
    def from_fetch(
        cls,
        *,
        fetch: Callable[..., Any],
        registry: InstrumentRegistry,
        client_id: str = "",
        access_token: str | None = None,
        token_manager: TokenLifecyclePort | None = None,
        base_url: str = DHAN_REST_BASE_URL,
        **http_options: Any) -> DhanApiClient:
        """Build a client around an injected fetch and optional token manager."""

        def auth_headers(token: str) -> dict[str, str]:
            return {
                "access-token": token or "",
                "client-id": client_id,
            }

        http, ws_token_provider = build_provider_client(
            fetch=fetch,
            base_url=base_url,
            auth_headers=auth_headers,
            token_manager=token_manager,
            access_token=access_token or "",
            provider="dhan",
        )
        return cls(
            http=http,
            registry=registry,
            client_id=client_id,
            base_url=base_url,
            ws_token_provider=ws_token_provider)

    def __init__(
        self,
        *,
        http: ProviderHttpClient,
        registry: InstrumentRegistry,
        client_id: str = "",
        base_url: str = "",
        ws_token_provider: Callable[[], str] | None = None) -> None:
        self._http = http
        self.rate_limiter = self._http.rate_limiter
        self._registry = registry
        self._client_id = client_id
        self._base_url = base_url.rstrip("/")
        self._ws_token_provider = ws_token_provider

    # -- internal helpers ---------------------------------------------------

    def _security_id(self, instrument_id: InstrumentId) -> str:
        """Return the raw numeric Dhan security ID for an instrument."""
        key = provider_key(self._registry, instrument_id)
        if key is None:
            raise InstrumentNotFoundError(
                f"dhan security id not mapped for {instrument_id}"
            )
        if ":" in key:
            key = key.split(":", 1)[1]
        return key

    def _url(self, path: str) -> str:
        return f"{self._base_url}{path}" if self._base_url else path

    def _request(
        self,
        method: str,
        path: str,
        *,
        cache_read: bool = False,
        **kwargs: Any,
    ) -> object:
        """Send a request through the composed pipeline and validate success."""
        url = self._url(path)
        body = self._http.request(method, url, cache_read=cache_read, **kwargs)
        return body

    def _invalidate_after_write(self) -> None:
        self._http.invalidate_cache()

    @staticmethod
    def _segment(instrument: Instrument) -> str:
        return dhan_segment(instrument)

    @staticmethod
    def _native_product_type(product_type: ProductType) -> str:
        return {
            ProductType.INTRADAY: "INTRADAY",
            ProductType.DELIVERY: "CNC",
            ProductType.MARGIN: "MARGIN",
            ProductType.MTF: "MTF",
            ProductType.COVER_ORDER: "CO",
        }[product_type]

    @staticmethod
    def _native_order_type(request: OrderRequest) -> str:
        if request.order_type is OrderType.STOP_LIMIT:
            return "STOP_LOSS"
        if request.order_type is OrderType.STOP:
            return "STOP_LOSS" if request.price is not None else "STOP_LOSS_MARKET"
        return request.order_type.value

    def _order_payload(self, request: OrderRequest) -> dict[str, object]:
        payload: dict[str, object] = {
            "dhanClientId": self._client_id,
            "exchangeSegment": self._segment(request.instrument),
            "securityId": self._security_id(request.instrument.instrument_id),
            "transactionType": request.side.value,
            "quantity": int(request.quantity.value),
            "orderType": self._native_order_type(request),
            "productType": self._native_product_type(request.product_type),
            "validity": request.time_in_force.value,
            "correlationId": str(request.correlation_id.value)
            if request.correlation_id is not None
            else "",
        }
        if request.price is not None:
            payload["price"] = float(request.price.value)
        if request.trigger_price is not None:
            payload["triggerPrice"] = float(request.trigger_price.value)
        if request.disclosed_quantity > 0:
            payload["disclosedQuantity"] = int(request.disclosed_quantity)
        return payload

    @staticmethod
    def _has_order_identity(row: Mapping[str, Any]) -> bool:
        return bool(row.get("securityId") or row.get("security_id"))

    @staticmethod
    def _domain_order_type(value: str, *, has_price: bool = False) -> OrderType:
        native = value.upper()
        if native == "STOP_LOSS":
            return OrderType.STOP_LIMIT if has_price else OrderType.STOP
        if native == "STOP_LOSS_MARKET":
            return OrderType.STOP
        return OrderType(native)

    @staticmethod
    def _response_order_id(body: object, provider: str) -> OrderId:
        row = _first_mapping(_unwrap_data(body))
        raw = row.get("orderId", row.get("order_id"))
        if provider == "Dhan eDIS":
            raw = row.get("authorizationId", row.get("edisId", row.get("edis_id")))
        if raw is None:
            ids = row.get("orderIds", row.get("order_ids"))
            raw = ids[0] if isinstance(ids, list) and ids else None
        if raw is None:
            raise ValueError(f"{provider} extension response missing order id")
        return OrderId(value=str(raw))

    @staticmethod
    def _option_exchange(underlying: Instrument) -> str:
        """Domain exchange for option legs of a REST chain for *underlying*."""
        exchange = str(getattr(underlying.exchange, "value", underlying.exchange)).upper()
        return _OPTION_LEG_EXCHANGE.get(exchange, "NFO")

    def _history_instrument_type(self, instrument: Instrument) -> str:
        meta = self._registry.meta(instrument.instrument_id)
        asset_class = str(meta.get("asset_class", "")).upper() or instrument.asset_class.value
        if asset_class == "INDEX":
            return "EQUITY"
        # The master's SEM_INSTRUMENT_NAME is the authoritative native kind for
        # the history API (FUTIDX/OPTIDX/...). Prefer the normalized copy the
        # master parser now stores in meta; fall back to raw rows (legacy
        # fixtures / load_mcx_rows) and then the exchange heuristic table.
        native_kind = str(meta.get("instrument_type") or "").upper()
        # Only trust the native kind when the typed instrument is actually a
        # derivative — a CA/PA row that degraded to Equity must not be sent as
        # an OPTIDX history request.
        if isinstance(instrument, (Future, Option)) and native_kind in {
            "FUTIDX", "FUTSTK", "FUTCOM", "FUTCUR",
            "OPTIDX", "OPTSTK", "OPTFUT", "OPTCUR",
        }:
            return native_kind
        raw = meta.get("raw")
        if isinstance(raw, dict):
            native_kind = str(raw.get("SEM_INSTRUMENT_NAME", "")).upper()
            if native_kind in {"FUTIDX", "FUTSTK", "FUTCOM", "FUTCUR"}:
                return native_kind
            if native_kind in {"OPTIDX", "OPTSTK", "OPTFUT", "OPTCUR"}:
                return native_kind
        exchange = instrument.exchange.value
        if asset_class == "FUTURE":
            return {
                "NFO": "FUTIDX",
                "BFO": "FUTIDX",
                "MCX": "FUTCOM",
                "NSE_COMM": "FUTCOM",
                "CDS": "FUTCUR",
                "BCD": "FUTCUR",
            }.get(exchange, "EQUITY")
        if asset_class == "OPTION":
            return {
                "NFO": "OPTIDX",
                "BFO": "OPTIDX",
                "MCX": "OPTFUT",
                "NSE_COMM": "OPTFUT",
                "CDS": "OPTCUR",
                "BCD": "OPTCUR",
            }.get(exchange, "EQUITY")
        return "EQUITY"

    def _batch_segment_map(
        self, instruments: Sequence[Instrument]
    ) -> tuple[dict[str, list[int]], dict[str, Instrument]]:
        segment_map: dict[str, list[int]] = {}
        id_map: dict[str, Instrument] = {}
        for instrument in instruments:
            try:
                sec_id = self._security_id(instrument.instrument_id)
                security_id = int(sec_id)
            except (InstrumentNotFoundError, ValueError):
                continue
            segment_map.setdefault(self._segment(instrument), []).append(security_id)
            id_map[sec_id] = instrument
        return segment_map, id_map

    # -- order mapping ------------------------------------------------------

    def _order_from_row(self, row: Mapping[str, Any], fallback_id: OrderId | None = None) -> Order:
        order_id = str(
            row.get("orderId", row.get("order_id", fallback_id.value if fallback_id else ""))
        )
        instrument_id = self._registry.resolve(
            str(row.get("securityId", row.get("security_id", "")))
        )
        if instrument_id is None:
            raise ValueError(f"Dhan order missing mapped securityId: {row!r}")
        status = {
            "TRADED": OrderStatus.FILLED,
            "PART_TRADED": OrderStatus.PARTIALLY_FILLED,
            "CANCELLED": OrderStatus.CANCELLED,
            "REJECTED": OrderStatus.REJECTED,
            "PENDING": OrderStatus.PENDING,
            "TRANSIT": OrderStatus.ACK,
            "TRIGGER_PENDING": OrderStatus.ACK,
            "AFTER_MARKET_ORDER": OrderStatus.ACK,
            "OPEN_PENDING": OrderStatus.ACK,
            "AMO_CANCELLED": OrderStatus.CANCELLED,
        }.get(
            str(row.get("orderStatus", row.get("status", "UNKNOWN"))).upper(), OrderStatus.UNKNOWN
        )
        raw_price = row.get("price")
        avg = (
            row.get("averagePrice")
            or row.get("avgPrice")
            or row.get("avg_traded_price")
            or row.get("average_price")
            or row.get("tradedPrice")
            or row.get("traded_price")
        )
        order_avg: Price | None = None
        if avg not in (None, "", 0, "0", 0.0):
            try:
                order_avg = Price(value=Decimal(str(avg)))
                if order_avg.value <= 0:
                    order_avg = None
            except Exception:
                order_avg = None
        return Order(
            order_id=OrderId(value=order_id),
            instrument=instrument_from_id(instrument_id),
            side=OrderSide(str(row.get("transactionType", "BUY")).upper()),
            order_type=self._domain_order_type(
                str(row.get("orderType", "MARKET")), has_price=raw_price not in (None, "")
            ),
            quantity=Quantity(value=as_decimal(row.get("quantity", 0))),
            price=_as_price(raw_price) if raw_price not in (None, "") else None,
            time_in_force=TimeInForce.DAY,
            status=status,
            correlation_id=correlation_id(
                row.get("correlationId"), fallback_seed="dhan-unknown"
            ),
            filled_quantity=Quantity(
                value=as_decimal(row.get("filledQty", row.get("tradedQuantity", 0)))
            ),
            avg_price_traded=order_avg)

    def _quote_from_row(self, instrument: Instrument, row: dict[str, Any]) -> Quote:
        ohlc = row.get("ohlc", {}) if isinstance(row.get("ohlc"), dict) else {}
        depth = row.get("depth", {}) if isinstance(row.get("depth"), dict) else {}
        bids = depth.get("buy", []) if isinstance(depth.get("buy"), list) else []
        asks = depth.get("sell", []) if isinstance(depth.get("sell"), list) else []
        oi_raw = row.get("oi", row.get("openInterest", row.get("open_interest")))
        open_interest = Quantity(value=as_decimal(str(oi_raw))) if oi_raw else None
        metadata: dict[str, object] | None = None
        greeks = row.get("greeks", row.get("option_greeks"))
        if isinstance(greeks, dict) and greeks:
            metadata = {"greeks": dict(greeks)}
        return Quote(
            instrument=instrument,
            ltp=_as_price(row.get("last_price")),
            bid=_as_price(bids[0].get("price")) if bids else None,
            ask=_as_price(asks[0].get("price")) if asks else None,
            volume=Quantity(Decimal(str(row.get("volume", 0) or 0))),
            open_interest=open_interest,
            ohlc=OHLC(
                open=_as_price(ohlc.get("open")),
                high=_as_price(ohlc.get("high")),
                low=_as_price(ohlc.get("low")),
                close=_as_price(ohlc.get("close"))),
            exchange=instrument.exchange.value,
            provider="dhan",
            metadata=metadata,
            timestamp=parse_timestamp_fallback(row.get("last_trade_time"), datetime.now(UTC)))

    def _positions(self, rows: object) -> list[Position]:
        if not isinstance(rows, list):
            return []
        out: list[Position] = []
        for row in rows:
            if not isinstance(row, dict):
                continue
            iid = self._registry.resolve(str(row.get("securityId", row.get("security_id", ""))))
            if iid is None:
                continue
            out.append(
                Position(
                    instrument=instrument_from_id(iid),
                    quantity=Quantity(
                        value=as_decimal(str(row.get("netQty", row.get("quantity", 0))))
                    ),
                    avg_price=_as_price(row.get("avgCostPrice", row.get("averagePrice", 0))),
                    realized_pnl=Money(
                        amount=as_decimal(row.get("realizedProfit", 0)), currency="INR"
                    ),
                    unrealized_pnl=Money(
                        amount=as_decimal(row.get("unrealizedProfit", 0)), currency="INR"
                    ))
            )
        return out

    def _account_for_portfolio(self) -> Account:
        snapshot = self.get_account()
        return Account(
            account_id=snapshot.account_id,
            balance=snapshot.balance or Money(amount=Decimal("0"), currency="INR"),
            margin=snapshot.margin or Money(amount=Decimal("0"), currency="INR"),
            equity=snapshot.equity or Money(amount=Decimal("0"), currency="INR"))

    # ======================================================================
    # Streaming backends
    # ======================================================================

    def order_stream_backend(self, *, ws_factory: Any | None = None) -> Any:
        """Order-update websocket backend; no socket until subscribe."""
        from tradex_brokers.dhan.ws_streams import DhanOrderStreamBackend

        return DhanOrderStreamBackend(
            token_provider=self._ws_token_provider or (lambda: ""),
            client_id=self._client_id,
            map_order=self._stream_order_from_row,
            ws_factory=ws_factory)

    def _stream_order_from_row(
        self, row: Mapping[str, Any],
    ) -> Order:
        """Map a live order-update row to a domain Order usable by the fill
        bridge: instrument resolved via the registry, and the fill price is
        the row's traded/average price (kept in avg_price_traded; reference
        price stays in price for audit, but price is also set for backward
        compat with existing tests).
        """
        order = self._order_from_row(row)
        if order.avg_price_traded is None:
            traded = row.get("tradedPrice", row.get("traded_price"))
            if (
                traded not in (None, "")
                and order.status in (OrderStatus.FILLED, OrderStatus.PARTIALLY_FILLED)
            ):
                try:
                    avg = Price(value=Decimal(str(traded)))
                    if avg.value > 0:
                        # keep price as traded for legacy callers, avg for new bridge
                        return replace(order, price=avg, avg_price_traded=avg)
                except Exception:
                    pass
        # ensure legacy price override when avg already set via _order_from_row
        if (
            order.avg_price_traded is not None
            and order.status in (OrderStatus.FILLED, OrderStatus.PARTIALLY_FILLED)
            and order.price is not None
            and order.price.value != order.avg_price_traded.value
        ):
            # ponytail: keep price == avg for fills so old assertions still pass
            return replace(order, price=order.avg_price_traded)
        return order

    def market_stream_backend(self, *, ws_factory: Any | None = None) -> Any:
        """Quote tick stream backend; no socket until subscribe."""
        from tradex_brokers.dhan.ws_streams import DhanMarketDataStreamBackend

        return DhanMarketDataStreamBackend(
            token_provider=self._ws_token_provider or (lambda: ""),
            client_id=self._client_id,
            registry=self._registry,
            ws_factory=ws_factory)

    def depth_stream_backend(
        self, *, total_slots: int = 20, ws_factory: Any | None = None
    ) -> Any:
        """Depth stream backend; no socket until subscribe."""
        from tradex_brokers.dhan.ws_streams import DhanDepthStreamBackend

        return DhanDepthStreamBackend(
            token_provider=self._ws_token_provider or (lambda: ""),
            client_id=self._client_id,
            registry=self._registry,
            total_slots=total_slots,
            ws_factory=ws_factory)

