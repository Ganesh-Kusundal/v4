"""Dhan WebSocket quote and depth streams.

Provider WebSocket transports for portfolio/order streams:

* Dhan order-update stream: a token+clientId query-authenticated socket
  pushing native order rows as JSON.
* Dhan market-data stream: JSON frames via RequestCode 15.
* Dhan depth stream: binary frames via RequestCode 23.
"""

from __future__ import annotations

import json
import logging
import threading
import time
from collections.abc import Callable, Iterable, Mapping, Sequence
from typing import Any, cast

from tradex_domain.errors import (
    CapabilityNotSupportedError,
    SessionStateError,
)
from tradex_domain.execution import Order, Position
from tradex_domain.instruments import Instrument
from tradex_domain.market import Depth, Quote
from tradex_domain.value_objects import InstrumentId

from tradex_brokers.common.provider_common import instrument_from_id
from tradex_brokers.common.ws_reconnect import AutoReconnectMixin
from tradex_brokers.common.ws_shared import row_to_quote
from tradex_brokers.dhan.client import dhan_segment
from tradex_brokers.dhan.tick_parser import SEGMENT_EXCHANGE, parse_tick_frame

log = logging.getLogger(__name__)

WsFactory = Callable[[str], Any]
MapOrder = Callable[[Mapping[str, Any]], Order]
MapPosition = Callable[[Mapping[str, Any]], Position | None]

#: Official Dhan order-update websocket (dhanhq ``OrderUpdate`` endpoint).
DHAN_ORDER_UPDATE_WS_URL = "wss://api-feed.dhan.co/v2/orderUpdate"
#: Official Dhan quote/market-data websocket (RequestCode 15 feed).
DHAN_MARKET_DATA_WS_URL = "wss://api-feed.dhan.co"
#: Official Dhan depth-20 websocket (RequestCode 23 binary feed).
DHAN_DEPTH_20_WS_URL = "wss://depth-api-feed.dhan.co/twentydepth"


def default_ws_factory(url: str) -> Any:
    """Open a real synchronous WebSocket connection (``[live]`` extra)."""
    from websockets.sync.client import connect  # noqa: PLC0415 — optional dep

    return connect(url, max_size=2**20)


class DhanOrderStreamBackend(AutoReconnectMixin):
    """Dhan order-update websocket: token+clientId auth, JSON order rows.

    Order-update sockets push every update with no subscribe frame, so
    ``_resubscribe`` is a no-op; reconnection reopens with a fresh token.
    """

    def __init__(
        self,
        *,
        token_provider: Callable[[], str],
        client_id: str,
        map_order: MapOrder,
        ws_url: str = DHAN_ORDER_UPDATE_WS_URL,
        ws_factory: WsFactory | None = None,
        reconnect: bool = True,
    ) -> None:
        self._ws_url = ws_url
        self._token_provider = token_provider
        self._client_id = client_id
        self._map_order = map_order
        self._ws_factory = ws_factory or default_ws_factory
        self._order_handlers: dict[str, Callable[[Order], None]] = {}
        self._ws: Any | None = None
        self._closing = False
        self._state_lock = threading.RLock()
        self._connect_lock = threading.Lock()
        self._init_reconnect(reconnect=reconnect)

    def subscribe_quotes(self, instruments: object, handler: object) -> object:
        raise CapabilityNotSupportedError(
            "Dhan quote websocket feed lands in a later wave; use polling quotes"
        )

    def subscribe_positions(self, handler: object) -> object:
        raise CapabilityNotSupportedError(
            "Dhan order-update feed carries order updates only; poll positions"
        )

    def subscribe_orders(self, handler: Callable[[Order], None]) -> object:
        subscription_id = f"dhan-order-{id(handler)}"
        with self._state_lock:
            if self._closing:
                raise SessionStateError("Dhan order-update stream is closed")
            self._order_handlers[subscription_id] = handler
        self._ensure_ws()
        return subscription_id

    def unsubscribe(self, subscription: str) -> None:
        with self._state_lock:
            self._order_handlers.pop(subscription, None)

    def close(self) -> None:
        with self._state_lock:
            self._closing = True
            ws = self._ws
            self._ws = None
        if ws is not None and hasattr(ws, "close"):
            ws.close()

    def _open_socket(self) -> Any:
        """Open a new order-update socket with a fresh token (reconnect)."""
        url = (
            f"{self._ws_url}?version=2"
            f"&token={self._token_provider()}&clientId={self._client_id}"
            f"&authType=2"
        )
        return self._ws_factory(url)

    def _resubscribe(self, ws: Any) -> None:
        """Order-update sockets push every update; there is nothing to replay."""

    def _ensure_ws(self) -> None:
        with self._connect_lock:
            with self._state_lock:
                if self._closing or self._ws is not None or self._ws_factory is None:
                    return
                if self._reconnect_thread is not None:  # reconnect pending
                    return
            ws = self._open_socket()
            with self._state_lock:
                if self._closing:
                    if hasattr(ws, "close"):
                        ws.close()
                    return
                self._ws = ws
            self._last_success_at = time.monotonic()
            self._reconnect.reset()
            if hasattr(ws, "recv"):
                threading.Thread(target=self._receive_loop, daemon=True).start()

    def _receive_loop(self) -> None:
        ws = self._ws
        while ws is not None and ws is self._ws:
            try:
                raw = ws.recv()
            except Exception:  # noqa: BLE001
                with self._state_lock:
                    closing = self._closing
                    current = self._ws is ws
                    if current:
                        self._ws = None
                if not closing and current:
                    log.warning("dhan_order_ws_recv_failed", exc_info=True)
                    self._schedule_reconnect()
                return
            try:
                self.feed_raw(raw)
            except Exception:  # noqa: BLE001
                log.warning("dhan_order_frame_dispatch_failed", exc_info=True)

    def feed_raw(self, raw: bytes | str) -> None:
        """Decode one JSON order-update frame (single row or list) and dispatch."""
        if isinstance(raw, (bytes, bytearray)):
            try:
                raw = raw.decode("utf-8")
            except UnicodeDecodeError:
                return
        try:
            msg = json.loads(raw)
        except (json.JSONDecodeError, TypeError):
            return
        rows = msg if isinstance(msg, list) else [msg]
        for row in rows:
            if not isinstance(row, dict):
                continue
            if "orderId" not in row and str(row.get("type") or "") != "order":
                continue
            try:
                order = self._map_order(row)
            except Exception:  # noqa: BLE001
                log.warning("dhan_order_update_unmapped", exc_info=True)
                continue
            for handler in tuple(self._order_handlers.values()):
                try:
                    handler(order)
                except Exception:  # noqa: BLE001
                    log.warning("stream_handler_failed", exc_info=True)


# ---------------------------------------------------------------------------
# Market-data streaming backends
# ---------------------------------------------------------------------------


def _shape_dhan_quote_row(payload: dict[str, Any]) -> dict[str, Any]:
    """Shape a raw Dhan quote WS payload into a REST-shaped row dict."""
    row: dict[str, Any] = {
        "last_price": payload.get("last_price", 0),
        "timestamp": payload.get("last_trade_time", payload.get("timestamp")),
        "depth": payload.get("depth", {"buy": [], "sell": []}),
        "ohlc": payload.get("ohlc"),
        "volume": payload.get("volume"),
        "oi": payload.get("oi"),
    }
    greeks = payload.get("greeks", payload.get("option_greeks"))
    if isinstance(greeks, dict) and greeks:
        row["greeks"] = dict(greeks)
    return row


class DhanMarketDataStreamBackend(AutoReconnectMixin):
    """Dhan quote/market-data stream: JSON frames via RequestCode 15."""

    def __init__(
        self,
        *,
        token_provider: Callable[[], str],
        client_id: str,
        registry: Any,
        ws_url: str = DHAN_MARKET_DATA_WS_URL,
        ws_factory: WsFactory | None = None,
        reconnect: bool = True,
    ) -> None:
        self._ws_url = ws_url
        self._token_provider = token_provider
        self._client_id = client_id
        self._registry = registry
        self._ws_factory = ws_factory or default_ws_factory
        self._quote_handlers: dict[str, Callable[[Quote], None]] = {}
        #: Canonical live wire set (replayed on reconnect) and per-subscription
        #: bookkeeping so ``unsubscribe`` prunes instruments whose last handler
        #: released them instead of letting ghosts accumulate forever. Keyed by
        #: ``InstrumentId`` — ``Instrument`` is not hashable.
        self._subscribed_instruments: list[Instrument] = []
        self._sub_instruments: dict[str, set[InstrumentId]] = {}
        self._instrument_refs: dict[InstrumentId, int] = {}
        self._instrument_cache: dict[str, Instrument] = {}
        self._ws: Any | None = None
        self._closing = False
        self._state_lock = threading.RLock()
        self._connect_lock = threading.Lock()
        self._init_reconnect(reconnect=reconnect)

    def subscribe_quotes(
        self, instruments: Sequence[Instrument], handler: Callable[[Quote], None]
    ) -> str:
        subscription_id = f"dhan-market-{id(handler)}"
        with self._state_lock:
            if self._closing:
                raise SessionStateError("Dhan market-data stream is closed")
            self._quote_handlers[subscription_id] = handler
            known = self._sub_instruments.setdefault(subscription_id, set())
            new_instruments: list[Instrument] = []
            for inst in instruments:
                iid = inst.instrument_id
                if iid in known:
                    continue
                known.add(iid)
                self._instrument_refs[iid] = self._instrument_refs.get(iid, 0) + 1
                if inst not in self._subscribed_instruments:
                    self._subscribed_instruments.append(inst)
                    new_instruments.append(inst)
        self._ensure_ws()
        if new_instruments:
            self._send_subscribe(new_instruments)
        return subscription_id

    def unsubscribe(self, subscription: str) -> None:
        with self._state_lock:
            self._quote_handlers.pop(subscription, None)
            instruments = self._sub_instruments.pop(subscription, set())
            self._drop_instruments(instruments)

    def unsubscribe_instruments(self, instruments: Sequence[Instrument]) -> None:
        """Drop *instruments* from the live wire set (partial unsubscribe).

        Dhan's market feed has no per-instrument unsubscribe frame, so the
        pruning lands on the tracked wire set: reconnects replay only live
        instruments and the set stops growing toward the broker's cap.
        """
        with self._state_lock:
            self._drop_instruments(instruments)

    def _drop_instruments(self, instruments: Iterable[Instrument | InstrumentId]) -> None:
        """Decrement refs and prune instruments whose last holder released them."""
        for inst in instruments:
            # Instruments carry their id; bare ids pass through unchanged.
            iid = cast(InstrumentId, getattr(inst, "instrument_id", inst))
            refs = self._instrument_refs.get(iid, 0)
            if refs <= 1:
                self._instrument_refs.pop(iid, None)
                if any(i.instrument_id == iid for i in self._subscribed_instruments):
                    self._subscribed_instruments = [
                        i for i in self._subscribed_instruments if i.instrument_id != iid
                    ]
            else:
                self._instrument_refs[iid] = refs - 1

    def close(self) -> None:
        with self._state_lock:
            self._closing = True
            ws = self._ws
            self._ws = None
        if ws is not None and hasattr(ws, "close"):
            ws.close()

    def _open_socket(self) -> Any:
        """Open a new market socket with a fresh token (used on reconnect)."""
        url = (
            f"{self._ws_url}?version=2"
            f"&token={self._token_provider()}&clientId={self._client_id}"
            f"&authType=2"
        )
        return self._ws_factory(url)

    def _resubscribe(self, ws: Any) -> None:
        """Replay the live subscription set on a freshly opened socket."""
        # Snapshot under the state lock — subscribe/unsubscribe threads mutate
        # the wire set concurrently with the reconnect worker.
        with self._state_lock:
            instruments = list(self._subscribed_instruments)
        if instruments and hasattr(ws, "send"):
            self._send_subscribe(instruments)

    def _ensure_ws(self) -> None:
        with self._connect_lock:
            with self._state_lock:
                if self._closing or self._ws is not None or self._ws_factory is None:
                    return
                if self._reconnect_thread is not None:  # reconnect pending
                    return
            ws = self._open_socket()
            with self._state_lock:
                if self._closing:
                    if hasattr(ws, "close"):
                        ws.close()
                    return
                self._ws = ws
            self._last_success_at = time.monotonic()
            self._reconnect.reset()
            # Replay the full live subscription set (a fresh open must heal the
            # pre-outage set after backoff exhaustion) before the loop starts.
            try:
                self._resubscribe(ws)
            except Exception:  # noqa: BLE001 — best-effort replay
                log.warning("stream reopen resubscribe failed", exc_info=True)
            if hasattr(ws, "recv"):
                threading.Thread(target=self._receive_loop, daemon=True).start()

    def _send_subscribe(self, instruments: Sequence[Instrument]) -> None:
        ws = self._ws
        if ws is None or not hasattr(ws, "send"):
            return
        instrument_list: list[dict[str, str]] = []
        for instrument in instruments:
            key = self._registry.provider_key(instrument.instrument_id)
            if key is None:
                continue
            if ":" in str(key):
                key = str(key).split(":", 1)[1]
            segment = dhan_segment(instrument)
            instrument_list.append({"ExchangeSegment": segment, "SecurityId": str(key)})
        if not instrument_list:
            return
        ws.send(
            json.dumps(
                {
                    "RequestCode": 15,
                    "SubscriptionMode": 2,  # Full (quote + 5-level depth per tick)
                    "InstrumentCount": len(instrument_list),
                    "InstrumentList": instrument_list,
                }
            )
        )

    def _receive_loop(self) -> None:
        ws = self._ws
        while ws is not None and ws is self._ws:
            try:
                raw = ws.recv()
            except Exception:  # noqa: BLE001
                with self._state_lock:
                    closing = self._closing
                    current = self._ws is ws
                    if current:
                        self._ws = None
                if not closing and current:
                    log.warning("dhan_market_ws_recv_failed", exc_info=True)
                    self._schedule_reconnect()
                return
            try:
                self.feed_raw(raw)
            except Exception:  # noqa: BLE001
                log.warning("dhan_market_frame_dispatch_failed", exc_info=True)

    def _cached_instrument(
        self, key: str, segment: int | None = None
    ) -> Instrument | None:
        """Return a cached Instrument for *key*, building on miss.

        *key* is Dhan's bare numeric security id. Master rows register
        ``{exchange}:{security_id}`` keys, so a bare id first tries direct
        resolution and then falls back to the segment-qualified key using the
        exchange-segment code carried in the binary frame header.
        """
        if key in self._instrument_cache:
            return self._instrument_cache[key]
        instrument_id = self._registry.resolve(key)
        if instrument_id is None and segment is not None:
            exchange = SEGMENT_EXCHANGE.get(segment)
            if exchange is not None:
                instrument_id = self._registry.resolve(f"{exchange}:{key}")
        if instrument_id is None:
            return None
        instrument = instrument_from_id(instrument_id)
        self._instrument_cache[key] = instrument
        return instrument

    def _dispatch_row(self, row: dict[str, Any]) -> None:
        """Build a Quote from a REST-shaped row and fan out to handlers.

        Only frames carrying an LTP (ticker/quote/full) produce quotes; OI and
        prev-close frames have no price update and are skipped.
        """
        if "last_price" not in row:
            return
        security_id = row.get("security_id")
        if security_id is None:
            return
        instrument = self._cached_instrument(
            str(security_id), segment=row.get("exchange_segment")
        )
        if instrument is None:
            return
        quote = row_to_quote(instrument, row, provider="dhan")
        for handler in tuple(self._quote_handlers.values()):
            try:
                handler(quote)
            except Exception:  # noqa: BLE001
                log.warning("stream_handler_failed", exc_info=True)

    def _handle_server_disconnect(self) -> None:
        """Close the socket on a server disconnect frame and schedule reconnect.

        Dhan throttles connections by closing the feed with a disconnect
        frame. Without handling it the socket was left in limbo with no
        reconnect armed (the exact Dhan throttle signature observed live).
        """
        with self._state_lock:
            ws = self._ws
            self._ws = None
        if ws is not None and hasattr(ws, "close"):
            try:
                ws.close()
            except Exception:  # noqa: BLE001 — teardown
                pass
        self._schedule_reconnect()

    def feed_raw(self, raw: bytes | str) -> None:
        """Decode one Dhan market-data frame and dispatch to handlers.

        The RequestCode-15 socket streams *binary* tick packets (see
        :mod:`tradex_brokers.dhan.tick_parser`); a small JSON path is kept for
        textual frames. Order-update frames (``orderId`` / type ``order``) are
        skipped — the order feed is serviced by :class:`DhanOrderStreamBackend`.
        """
        if isinstance(raw, (bytes, bytearray)):
            row = parse_tick_frame(bytes(raw))
            if row is not None:
                if row.get("type") == "disconnect":
                    log.warning(
                        "dhan_market_feed_disconnect code=%s", row.get("error_code")
                    )
                    self._handle_server_disconnect()
                    return
                self._dispatch_row(row)
                return
            # Not a recognized binary frame — try the legacy JSON path.
            try:
                text = raw.decode("utf-8")
            except UnicodeDecodeError:
                return
            raw = text
        try:
            msg = json.loads(raw)
        except (json.JSONDecodeError, TypeError):
            return
        if not isinstance(msg, dict):
            return
        if "orderId" in msg or str(msg.get("type") or "") == "order":
            return
        segment = msg.get("segment") or msg.get("ExchangeSegment") or ""
        sec_id = msg.get("securityId") or msg.get("SecurityId") or ""
        if not segment or not sec_id:
            return
        instrument = self._cached_instrument(str(sec_id))
        if instrument is None:
            return
        row = _shape_dhan_quote_row(msg)
        quote = row_to_quote(instrument, row, provider="dhan")
        for handler in tuple(self._quote_handlers.values()):
            try:
                handler(quote)
            except Exception:  # noqa: BLE001
                log.warning("stream_handler_failed", exc_info=True)


class DhanDepthStreamBackend(AutoReconnectMixin):
    """Dhan dedicated depth stream: binary frames via RequestCode 23."""

    def __init__(
        self,
        *,
        token_provider: Callable[[], str],
        client_id: str,
        registry: Any,
        ws_url: str = DHAN_DEPTH_20_WS_URL,
        total_slots: int = 20,
        ws_factory: WsFactory | None = None,
        reconnect: bool = True,
    ) -> None:
        self._ws_url = ws_url
        self._token_provider = token_provider
        self._client_id = client_id
        self._registry = registry
        self._total_slots = total_slots
        self._header_carries_security_id = total_slots <= 20
        self._ws_factory = ws_factory or default_ws_factory
        self._depth_handlers: dict[str, Callable[[Depth], None]] = {}
        #: Canonical live wire set (replayed on reconnect) and per-subscription
        #: bookkeeping so ``unsubscribe`` prunes instruments whose last handler
        #: released them instead of letting ghosts accumulate forever. Keyed by
        #: ``InstrumentId`` — ``Instrument`` is not hashable.
        self._subscribed_instruments: list[Instrument] = []
        self._sub_instruments: dict[str, set[InstrumentId]] = {}
        self._instrument_refs: dict[InstrumentId, int] = {}
        self._instrument_cache: dict[str, Instrument] = {}
        self._ws: Any | None = None
        self._closing = False
        self._state_lock = threading.RLock()
        self._connect_lock = threading.Lock()
        self._init_reconnect(reconnect=reconnect)

    def subscribe_depth(
        self, instruments: Sequence[Instrument], handler: Callable[[Depth], None]
    ) -> str:
        subscription_id = f"dhan-depth-{id(handler)}"
        with self._state_lock:
            if self._closing:
                raise SessionStateError("Dhan depth stream is closed")
            self._depth_handlers[subscription_id] = handler
            known = self._sub_instruments.setdefault(subscription_id, set())
            new_instruments: list[Instrument] = []
            for inst in instruments:
                iid = inst.instrument_id
                if iid in known:
                    continue
                known.add(iid)
                self._instrument_refs[iid] = self._instrument_refs.get(iid, 0) + 1
                if inst not in self._subscribed_instruments:
                    self._subscribed_instruments.append(inst)
                    new_instruments.append(inst)
        self._ensure_ws()
        if new_instruments:
            self._send_subscribe(new_instruments)
        return subscription_id

    def unsubscribe(self, subscription: str) -> None:
        with self._state_lock:
            self._depth_handlers.pop(subscription, None)
            instruments = self._sub_instruments.pop(subscription, set())
            self._drop_instruments(instruments)

    def unsubscribe_instruments(self, instruments: Sequence[Instrument]) -> None:
        """Drop *instruments* from the live wire set (partial unsubscribe).

        Dhan's depth feed has no per-instrument unsubscribe frame, so the
        pruning lands on the tracked wire set: reconnects replay only live
        instruments and the set stops growing toward the broker's cap.
        """
        with self._state_lock:
            self._drop_instruments(instruments)

    def _drop_instruments(self, instruments: Iterable[Instrument | InstrumentId]) -> None:
        """Decrement refs and prune instruments whose last holder released them."""
        for inst in instruments:
            # Instruments carry their id; bare ids pass through unchanged.
            iid = cast(InstrumentId, getattr(inst, "instrument_id", inst))
            refs = self._instrument_refs.get(iid, 0)
            if refs <= 1:
                self._instrument_refs.pop(iid, None)
                if any(i.instrument_id == iid for i in self._subscribed_instruments):
                    self._subscribed_instruments = [
                        i for i in self._subscribed_instruments if i.instrument_id != iid
                    ]
            else:
                self._instrument_refs[iid] = refs - 1

    def close(self) -> None:
        with self._state_lock:
            self._closing = True
            ws = self._ws
            self._ws = None
        if ws is not None and hasattr(ws, "close"):
            ws.close()

    def _open_socket(self) -> Any:
        """Open a new depth socket with a fresh token (used on reconnect)."""
        url = (
            f"{self._ws_url}?token={self._token_provider()}"
            f"&clientId={self._client_id}&authType=2"
        )
        return self._ws_factory(url)

    def _resubscribe(self, ws: Any) -> None:
        """Replay the live subscription set on a freshly opened socket."""
        # Snapshot under the state lock — subscribe/unsubscribe threads mutate
        # the wire set concurrently with the reconnect worker.
        with self._state_lock:
            instruments = list(self._subscribed_instruments)
        if instruments and hasattr(ws, "send"):
            self._send_subscribe(instruments)

    def _ensure_ws(self) -> None:
        with self._connect_lock:
            with self._state_lock:
                if self._closing or self._ws is not None or self._ws_factory is None:
                    return
                if self._reconnect_thread is not None:  # reconnect pending
                    return
            ws = self._open_socket()
            with self._state_lock:
                if self._closing:
                    if hasattr(ws, "close"):
                        ws.close()
                    return
                self._ws = ws
            self._last_success_at = time.monotonic()
            self._reconnect.reset()
            # Replay the full live subscription set (a fresh open must heal the
            # pre-outage set after backoff exhaustion) before the loop starts.
            try:
                self._resubscribe(ws)
            except Exception:  # noqa: BLE001 — best-effort replay
                log.warning("stream reopen resubscribe failed", exc_info=True)
            if hasattr(ws, "recv"):
                threading.Thread(target=self._receive_loop, daemon=True).start()

    def _send_subscribe(self, instruments: Sequence[Instrument]) -> None:
        ws = self._ws
        if ws is None or not hasattr(ws, "send"):
            return
        instrument_list: list[dict[str, str]] = []
        for instrument in instruments:
            key = self._registry.provider_key(instrument.instrument_id)
            if key is None:
                continue
            if ":" in key:
                key = key.split(":", 1)[1]
            segment = dhan_segment(instrument)
            instrument_list.append({"ExchangeSegment": segment, "SecurityId": str(key)})
        if not instrument_list:
            return
        ws.send(
            json.dumps(
                {
                    "RequestCode": 23,
                    "InstrumentCount": len(instrument_list),
                    "InstrumentList": instrument_list,
                }
            )
        )

    def _receive_loop(self) -> None:
        ws = self._ws
        while ws is not None and ws is self._ws:
            try:
                raw = ws.recv()
            except Exception:  # noqa: BLE001
                with self._state_lock:
                    closing = self._closing
                    current = self._ws is ws
                    if current:
                        self._ws = None
                if not closing and current:
                    log.warning("dhan_depth_ws_recv_failed", exc_info=True)
                    self._schedule_reconnect()
                return
            try:
                if isinstance(raw, (bytes, bytearray)):
                    self.feed_raw(bytes(raw))
            except Exception:  # noqa: BLE001
                log.warning("dhan_depth_frame_dispatch_failed", exc_info=True)

    def _cached_instrument(self, key: str) -> Instrument | None:
        if key in self._instrument_cache:
            return self._instrument_cache[key]
        instrument_id = self._registry.resolve(key)
        if instrument_id is None:
            return None
        instrument = instrument_from_id(instrument_id)
        self._instrument_cache[key] = instrument
        return instrument

    def feed_raw(self, raw: bytes) -> None:
        """Decode one binary depth frame and dispatch to handlers."""
        from tradex_brokers.dhan.depth_parser import parse_depth_frame

        packets = parse_depth_frame(
            raw,
            total_slots=self._total_slots,
            header_carries_security_id=self._header_carries_security_id,
        )
        if not packets:
            return
        by_security: dict[int, dict[str, list[Any]]] = {}
        for pkt in packets:
            sec_id = pkt["security_id"]
            by_security.setdefault(sec_id, {"bids": [], "asks": []})
            target = by_security[sec_id]
            if pkt["side"] == "bids":
                target["bids"].extend(pkt["levels"])
            else:
                target["asks"].extend(pkt["levels"])
        for sec_id, sides in by_security.items():
            instrument = self._cached_instrument(str(sec_id))
            if instrument is None:
                continue
            depth = Depth(
                instrument=instrument,
                bids=tuple(
                    sorted(sides["bids"], key=lambda level: level[0].value, reverse=True)
                ),
                asks=tuple(sorted(sides["asks"], key=lambda level: level[0].value)),
            )
            for handler in tuple(self._depth_handlers.values()):
                try:
                    handler(depth)
                except Exception:  # noqa: BLE001
                    log.warning("stream_handler_failed", exc_info=True)


__all__ = [
    "DHAN_DEPTH_20_WS_URL",
    "DHAN_MARKET_DATA_WS_URL",
    "DHAN_ORDER_UPDATE_WS_URL",
    "DhanDepthStreamBackend",
    "DhanMarketDataStreamBackend",
    "DhanOrderStreamBackend",
    "default_ws_factory",
]
