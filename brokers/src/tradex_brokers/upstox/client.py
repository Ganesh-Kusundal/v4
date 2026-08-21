"""Upstox REST API client for the v4 broker adapter.

Owns Upstox endpoint paths and native field mapping.  Authentication, retry,
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
    OrderSide,
    OrderStatus,
    OrderType,
    ProductType,
    TimeInForce,
)
from tradex_domain.execution import Account, Order, OrderRequest, Position
from tradex_domain.instruments import Instrument
from tradex_domain.market import OHLC, Quote
from tradex_domain.value_objects import (
    Money,
    OrderId,
    Quantity,
)
from tradex_domain.wire import InstrumentRegistry

from tradex_brokers.common.client_shared import (
    build_provider_client,
    correlation_id,
    parse_timestamp_fallback,
)
from tradex_brokers.common.endpoints import (
    UPSTOX_HFT_BASE_URL,
    UPSTOX_REST_BASE_URL,
    UPSTOX_V3_BASE_URL,
)

from tradex_brokers.common.provider_client import ProviderHttpClient
from tradex_brokers.common.streaming import NullStreamBackend
from tradex_brokers.common.provider_common import (
    as_decimal,
    as_price,
    first_mapping,
    instrument_from_id,
    provider_key,
    unwrap_data,
)
from tradex_brokers.common.token_lifecycle import TokenLifecyclePort
from tradex_brokers.upstox._admin import AdminMixin
from tradex_brokers.upstox._alerts import AlertsMixin
from tradex_brokers.upstox._marketdata import MarketDataMixin
from tradex_brokers.upstox._orders import OrdersMixin
from tradex_brokers.upstox._portfolio import PortfolioMixin

# Shared value/response helpers — aliases into common/provider_common so the
# facade's private mapping helpers keep the short underscore names (DRY).
_as_price = as_price
_unwrap_data = unwrap_data
_first_mapping = first_mapping


class UpstoxApiClient(OrdersMixin, PortfolioMixin, MarketDataMixin, AlertsMixin, AdminMixin):
    """Upstox REST endpoint implementation for the v4 broker adapter."""

    @classmethod
    def from_fetch(
        cls,
        *,
        fetch: Callable[..., Any],
        registry: InstrumentRegistry,
        access_token: str = "",
        token_manager: TokenLifecyclePort | None = None,
        base_url: str = UPSTOX_REST_BASE_URL,
        base_hft: str = UPSTOX_HFT_BASE_URL,
        base_v3: str = UPSTOX_V3_BASE_URL,
        **http_options: Any) -> UpstoxApiClient:
        """Build a client around an injected fetch and optional token manager."""

        def auth_headers(token: str) -> dict[str, str]:
            return {
                "Authorization": f"Bearer {token or ''}",
            }

        http, ws_token_provider = build_provider_client(
            fetch=fetch,
            base_url=base_url,
            auth_headers=auth_headers,
            token_manager=token_manager,
            access_token=access_token,
            provider="upstox",
        )
        return cls(
            http=http,
            registry=registry,
            access_token=access_token,
            base_url=base_url,
            base_hft=base_hft,
            base_v3=base_v3,
            ws_fetch=fetch,
            ws_token_provider=ws_token_provider)

    def __init__(
        self,
        *,
        http: ProviderHttpClient,
        registry: InstrumentRegistry,
        access_token: str = "",
        base_url: str = UPSTOX_REST_BASE_URL,
        base_hft: str = UPSTOX_HFT_BASE_URL,
        base_v3: str = UPSTOX_V3_BASE_URL,
        ws_fetch: Callable[..., tuple[int, Any]] | None = None,
        ws_token_provider: Callable[[], str] | None = None) -> None:
        self._http = http
        self._registry = registry
        self._access_token = access_token
        self._base_url = base_url.rstrip("/")
        self._base_hft = base_hft.rstrip("/")
        self._base_v3 = base_v3.rstrip("/")
        self._ws_fetch = ws_fetch
        self._ws_token_provider = ws_token_provider

    # -- internal helpers ---------------------------------------------------

    def invalidate_read_cache(self) -> None:
        """Drop cached read responses so a verification probe hits the wire."""
        self._http.invalidate_cache()

    def _url(self, path: str, *, host: str = "v2") -> str:
        if path.startswith("http://") or path.startswith("https://"):
            return path
        base = {"v2": self._base_url, "hft": self._base_hft, "v3": self._base_v3}[host]
        return f"{base}{path}"

    def _request(
        self,
        method: str,
        path: str,
        *,
        host: str = "v2",
        cache_read: bool = False,
        **kwargs: Any) -> object:
        url = self._url(path, host=host)
        body = self._http.request(
            method,
            url,
            cache_read=cache_read,
            **kwargs)
        return body

    def _invalidate_after_write(self) -> None:
        self._http.invalidate_cache()

    @staticmethod
    def _native_order_type(request: OrderRequest) -> str:
        if request.order_type is OrderType.STOP_LIMIT:
            return "SL"
        if request.order_type is OrderType.STOP:
            return "SL" if request.price is not None else "SL-M"
        return request.order_type.value

    @staticmethod
    def _native_product_type(product_type: ProductType) -> str:
        product = {
            ProductType.INTRADAY: "I",
            ProductType.DELIVERY: "D",
            ProductType.MTF: "MTF",
            ProductType.COVER_ORDER: "CO",
        }.get(product_type)
        if product is None:
            raise ValueError(
                f"Upstox does not support product type {product_type.value!r}"
            )
        return product

    def _order_payload(self, request: OrderRequest) -> dict[str, object]:
        payload: dict[str, object] = {
            "instrument_token": provider_key(self._registry, request.instrument.instrument_id),
            "transaction_type": request.side.value,
            "quantity": int(request.quantity.value),
            "order_type": self._native_order_type(request),
            "product": self._native_product_type(request.product_type),
            "validity": request.time_in_force.value,
            "disclosed_quantity": int(request.disclosed_quantity),
            "market_protection": int(request.market_protection),
            "tag": str(request.correlation_id.value) if request.correlation_id is not None else "",
        }
        if request.price is not None:
            payload["price"] = float(request.price.value)
        if request.trigger_price is not None:
            payload["trigger_price"] = float(request.trigger_price.value)
        return payload

    @staticmethod
    def _domain_order_type(value: str) -> OrderType:
        native = value.upper()
        if native == "SL":
            return OrderType.STOP_LIMIT
        if native == "SL-M":
            return OrderType.STOP
        return OrderType(native)

    @staticmethod
    def _has_order_identity(row: Mapping[str, Any]) -> bool:
        return bool(row.get("instrument_token") or row.get("tradingsymbol"))

    @staticmethod
    def _response_order_id(body: object, provider: str) -> OrderId:
        row = _first_mapping(_unwrap_data(body))
        raw = row.get("order_id", row.get("orderId"))
        if raw is None:
            ids = row.get("order_ids", row.get("orderIds"))
            raw = ids[0] if isinstance(ids, list) and ids else None
        if raw is None:
            raise ValueError(f"{provider} extension response missing order id")
        return OrderId(value=str(raw))

    def _order_from_row(self, row: Mapping[str, Any], fallback_id: OrderId | None = None) -> Order:
        order_id = str(row.get("order_id", fallback_id.value if fallback_id else ""))
        instrument_id = self._registry.resolve(
            str(row.get("instrument_token", row.get("tradingsymbol", "")))
        )
        if instrument_id is None:
            raise ValueError(f"Upstox order missing mapped instrument token: {row!r}")
        status = {
            "complete": OrderStatus.FILLED,
            "open": OrderStatus.ACK,
            "pending": OrderStatus.PENDING,
            "rejected": OrderStatus.REJECTED,
            "cancelled": OrderStatus.CANCELLED,
            "trigger pending": OrderStatus.ACK,
            "queued": OrderStatus.ACK,
            "after_market_order_req_received": OrderStatus.ACK,
            "expired": OrderStatus.CANCELLED,
        }.get(str(row.get("status", "open")).lower(), OrderStatus.UNKNOWN)
        native_type = str(row.get("order_type", "MARKET")).upper()
        order_type = self._domain_order_type(native_type)
        return Order(
            order_id=OrderId(value=order_id),
            instrument=instrument_from_id(instrument_id),
            side=OrderSide(str(row.get("transaction_type", "BUY")).upper()),
            order_type=order_type,
            quantity=Quantity(value=as_decimal(str(row.get("quantity")))),
            price=_as_price(row["price"]) if row.get("price") not in (None, "") else None,
            time_in_force=TimeInForce.DAY,
            status=status,
            filled_quantity=Quantity(value=as_decimal(str(row.get("filled_quantity")))),
            correlation_id=correlation_id(row.get("tag"), fallback_seed="upstox-unknown"))

    def _stream_order_from_row(self, row: Mapping[str, Any]) -> Order:
        """Map a live order-update row, overriding price with traded price for fills."""
        order = self._order_from_row(row)
        traded = row.get("average_price")
        if (
            traded not in (None, "", 0)
            and order.status in (OrderStatus.FILLED, OrderStatus.PARTIALLY_FILLED)
        ):
            return replace(order, price=_as_price(traded))
        return order

    def _quote_from_row(self, instrument: Instrument, row: dict[str, Any]) -> Quote:
        depth = row.get("depth", {}) if isinstance(row.get("depth"), dict) else {}
        bids = depth.get("buy", []) if isinstance(depth.get("buy"), list) else []
        asks = depth.get("sell", []) if isinstance(depth.get("sell"), list) else []
        ohlc_data = row.get("ohlc", {}) if isinstance(row.get("ohlc"), dict) else {}
        ohlc_obj = OHLC(
            open=_as_price(ohlc_data.get("open")),
            high=_as_price(ohlc_data.get("high")),
            low=_as_price(ohlc_data.get("low")),
            close=_as_price(ohlc_data.get("close"))) if ohlc_data else None
        oi_raw = row.get("oi", row.get("open_interest"))
        open_interest = Quantity(value=as_decimal(str(oi_raw))) if oi_raw else None
        metadata: dict[str, object] | None = None
        greeks = row.get("greeks", row.get("option_greeks"))
        if isinstance(greeks, dict) and greeks:
            metadata = {"greeks": dict(greeks)}
        return Quote(
            instrument=instrument,
            ltp=_as_price(row.get("last_price", row.get("ltp"))),
            bid=_as_price(bids[0].get("price")) if bids else None,
            ask=_as_price(asks[0].get("price")) if asks else None,
            volume=Quantity(Decimal(str(row.get("volume", 0) or 0))),
            open_interest=open_interest,
            ohlc=ohlc_obj,
            exchange=instrument.exchange.value,
            provider="upstox",
            metadata=metadata,
            timestamp=parse_timestamp_fallback(row.get("timestamp"), datetime.now(UTC)))

    def _positions(self, rows: object) -> list[Position]:
        if not isinstance(rows, list):
            return []
        out: list[Position] = []
        for row in rows:
            if not isinstance(row, dict):
                continue
            iid = self._registry.resolve(
                str(row.get("instrument_token", row.get("tradingsymbol", "")))
            )
            if iid is None:
                continue
            out.append(
                Position(
                    instrument=instrument_from_id(iid),
                    quantity=Quantity(
                        value=as_decimal(str(row.get("quantity", row.get("day_buy_quantity"))))
                    ),
                    avg_price=_as_price(row.get("average_price")),
                    realized_pnl=Money(
                        amount=as_decimal(str(row.get("realised"))), currency="INR"
                    ),
                    unrealized_pnl=Money(
                        amount=as_decimal(str(row.get("unrealised"))), currency="INR"
                    ))
            )
        return out

    @staticmethod
    def _market_row(raw: object, key: str) -> dict[str, Any]:
        """Resolve the per-instrument row from a market-quote payload."""
        if not isinstance(raw, dict):
            return {}
        row = raw.get(key, {})
        if isinstance(row, dict) and row:
            return row
        if len(raw) == 1:
            only = next(iter(raw.values()))
            if isinstance(only, dict):
                return only
        return row if isinstance(row, dict) else {}

    def _resolve_batch_keys(
        self, instruments: Sequence[Instrument]
    ) -> tuple[list[str], dict[str, Instrument]]:
        """Registry keys for the batch endpoints."""
        keys: list[str] = []
        key_map: dict[str, Instrument] = {}
        for instrument in instruments:
            key = self._registry.provider_key(instrument.instrument_id)
            if key is None:
                continue
            keys.append(key)
            key_map[key] = instrument
        return keys, key_map

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

    def portfolio_stream_backend(self, *, ws_factory: Any | None = None) -> Any:
        """Authorized order/position stream; no socket until subscribe.

        Materializes a real ``UpstoxPortfolioStreamBackend`` when the client
        has a WS transport (``ws_fetch``/``ws_token_provider``, wired by
        ``from_fetch``). Falls back to a ``NullStreamBackend`` when no WS
        transport exists (e.g. HTTP-only test clients).
        """
        if self._ws_fetch is None or self._ws_token_provider is None:
            return NullStreamBackend()
        from tradex_brokers.upstox.ws_streams import (  # noqa: PLC0415
            UPSTOX_PORTFOLIO_AUTHORIZE_PATH,
            UpstoxPortfolioStreamBackend,
        )

        return UpstoxPortfolioStreamBackend(
            authorize_url=f"{self._base_url}{UPSTOX_PORTFOLIO_AUTHORIZE_PATH}",
            ws_fetch=self._ws_fetch,
            token_provider=self._ws_token_provider,
            map_order=lambda row: self._stream_order_from_row(dict(row)),
            map_position=lambda row: next(iter(self._positions([dict(row)])), None),
            ws_factory=ws_factory)

    def market_stream_backend(self, *, ws_factory: Any | None = None) -> Any:
        """Authorized market-data stream; no socket until subscribe.

        Materializes a real ``UpstoxMarketDataStreamBackend`` when the client
        has a WS transport (``ws_fetch``/``ws_token_provider``, wired by
        ``from_fetch``). Falls back to a ``NullStreamBackend`` when no WS
        transport exists (e.g. HTTP-only test clients) — parity with
        ``portfolio_stream_backend``.
        """
        if self._ws_fetch is None or self._ws_token_provider is None:
            return NullStreamBackend()
        from tradex_brokers.upstox.ws_streams import (  # noqa: PLC0415
            UPSTOX_MARKET_DATA_AUTHORIZE_PATH,
            UpstoxMarketDataStreamBackend,
        )

        return UpstoxMarketDataStreamBackend(
            authorize_url=f"{self._base_v3}{UPSTOX_MARKET_DATA_AUTHORIZE_PATH}",
            ws_fetch=self._ws_fetch,
            token_provider=self._ws_token_provider,
            registry=self._registry,
            ws_factory=ws_factory)

    def order_stream_backend(self, *, ws_factory: Any | None = None) -> Any:
        """Order-update websocket backend; no socket until subscribe."""
        return self.portfolio_stream_backend(ws_factory=ws_factory)


__all__ = ["UpstoxApiClient"]
