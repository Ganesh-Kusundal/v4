"""Upstox WebSocket quote and depth streams.

Provider WebSocket transports for portfolio/order streams:

* Upstox portfolio stream: a REST authorize call returns a short-lived
  ``authorized_redirect_uri``; the socket then pushes plain JSON text frames.
* Upstox market-data stream: binary protobuf frames decoded by ws_decoder.
"""

from __future__ import annotations

import json
import logging
import threading
import time
from collections.abc import Callable, Iterable, Mapping, Sequence
from typing import Any, cast

from tradex_domain.errors import (
    BrokerUnavailableError,
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

logger = logging.getLogger(__name__)

WsFactory = Callable[[str], Any]
MapOrder = Callable[[Mapping[str, Any]], Order]
MapPosition = Callable[[Mapping[str, Any]], Position | None]

#: Official Upstox V3 market-data feed authorize endpoint path.
UPSTOX_MARKET_DATA_AUTHORIZE_PATH = "/feed/market-data-feed/authorize"
#: Official Upstox V2 portfolio/order stream authorize endpoint path.
UPSTOX_PORTFOLIO_AUTHORIZE_PATH = "/feed/portfolio-stream-feed/authorize"


def default_ws_factory(url: str) -> Any:
    """Open a real synchronous WebSocket connection (``[live]`` extra)."""
    from websockets.sync.client import connect  # noqa: PLC0415 — optional dep

    return connect(url, max_size=2**20)


class UpstoxPortfolioStreamBackend(AutoReconnectMixin):
    """Authorized Upstox portfolio stream: JSON order/position updates.

    Portfolio sockets push every update with no subscribe frame, so
    ``_resubscribe`` is a no-op; reconnection re-authorizes with a fresh
    token and opens a new socket.
    """

    def __init__(
        self,
        *,
        authorize_url: str,
        ws_fetch: Callable[..., tuple[int, Any]],
        token_provider: Callable[[], str],
        map_order: MapOrder,
        map_position: MapPosition | None = None,
        ws_factory: WsFactory | None = None,
        reconnect: bool = True,
    ) -> None:
        self._authorize_url = authorize_url
        self._ws_fetch = ws_fetch
        self._token_provider = token_provider
        self._map_order = map_order
        self._map_position = map_position
        self._ws_factory = ws_factory or default_ws_factory
        self._order_handlers: dict[str, Callable[[Order], None]] = {}
        self._position_handlers: dict[str, Callable[[Position], None]] = {}
        self._ws: Any | None = None
        self._closing = False
        self._state_lock = threading.RLock()
        self._connect_lock = threading.Lock()
        self._init_reconnect(reconnect=reconnect)

    def subscribe_quotes(self, instruments: object, handler: object) -> object:
        raise CapabilityNotSupportedError(
            "Upstox quote websocket feed lands in a later wave; use polling quotes"
        )

    def subscribe_orders(self, handler: Callable[[Order], None]) -> str:
        subscription_id = f"upstox-order-{id(handler)}"
        with self._state_lock:
            if self._closing:
                raise SessionStateError("Upstox portfolio stream is closed")
            self._order_handlers[subscription_id] = handler
        self._ensure_ws()
        return subscription_id

    def subscribe_positions(self, handler: Callable[[Position], None]) -> str:
        subscription_id = f"upstox-pos-{id(handler)}"
        with self._state_lock:
            if self._closing:
                raise SessionStateError("Upstox portfolio stream is closed")
            self._position_handlers[subscription_id] = handler
        self._ensure_ws()
        return subscription_id

    def unsubscribe(self, subscription: str) -> None:
        with self._state_lock:
            self._order_handlers.pop(subscription, None)
            self._position_handlers.pop(subscription, None)

    def close(self) -> None:
        with self._state_lock:
            self._closing = True
            ws = self._ws
            self._ws = None
        if ws is not None and hasattr(ws, "close"):
            ws.close()

    def _authorize_ws_url(self) -> str:
        token = self._token_provider()
        status, body = self._ws_fetch(
            "GET",
            self._authorize_url,
            headers={"Authorization": f"Bearer {token}", "Accept": "application/json"},
        )
        data = body.get("data") if isinstance(body, dict) else None
        url: object = None
        if isinstance(data, dict):
            url = data.get("authorized_redirect_uri") or data.get("authorizedRedirectUri")
        if status != 200 or not isinstance(url, str) or not url:
            raise BrokerUnavailableError(
                f"Upstox portfolio stream authorization failed (HTTP {status})"
            )
        return url

    def _open_socket(self) -> Any:
        """Authorize (fresh token) and open a new portfolio socket (reconnect)."""
        return self._ws_factory(self._authorize_ws_url())

    def _resubscribe(self, ws: Any) -> None:
        """Portfolio sockets push every update; there is nothing to replay."""

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
                    logger.warning("upstox_portfolio_ws_recv_failed", exc_info=True)
                    self._schedule_reconnect()
                return
            try:
                self.feed_raw(raw)
            except Exception:  # noqa: BLE001
                logger.warning("upstox_portfolio_frame_dispatch_failed", exc_info=True)

    def feed_raw(self, raw: bytes | str) -> None:
        """Decode one JSON portfolio-stream frame and dispatch to handlers."""
        if isinstance(raw, (bytes, bytearray)):
            try:
                raw = raw.decode("utf-8")
            except UnicodeDecodeError:
                return
        try:
            msg = json.loads(raw)
        except (json.JSONDecodeError, TypeError):
            return
        if not isinstance(msg, dict):
            return
        update_type = str(msg.get("type") or msg.get("update_type") or "").lower()
        payload = msg.get("data")
        if not isinstance(payload, dict):
            return
        if "order" in update_type:
            try:
                order = self._map_order(payload)
            except Exception:  # noqa: BLE001
                logger.warning("upstox_order_update_unmapped", exc_info=True)
                return
            for handler in tuple(self._order_handlers.values()):
                try:
                    handler(order)
                except Exception:  # noqa: BLE001
                    logger.warning("stream_handler_failed", exc_info=True)
        elif "position" in update_type and self._map_position is not None:
            try:
                position = self._map_position(payload)
            except Exception:  # noqa: BLE001
                logger.warning("upstox_position_update_unmapped", exc_info=True)
                return
            if position is not None:
                for pos_handler in tuple(self._position_handlers.values()):
                    try:
                        pos_handler(position)
                    except Exception:  # noqa: BLE001
                        logger.warning("stream_handler_failed", exc_info=True)


class UpstoxMarketDataStreamBackend(AutoReconnectMixin):
    """Authorized Upstox V3 market-data stream: binary protobuf frames."""

    def __init__(
        self,
        *,
        authorize_url: str,
        ws_fetch: Callable[..., tuple[int, Any]],
        token_provider: Callable[[], str],
        registry: Any,
        ws_factory: WsFactory | None = None,
        reconnect: bool = True,
    ) -> None:
        self._authorize_url = authorize_url
        self._ws_fetch = ws_fetch
        self._token_provider = token_provider
        self._registry = registry
        self._ws_factory = ws_factory or default_ws_factory
        self._quote_handlers: dict[str, Callable[[Quote], None]] = {}
        self._depth_handlers: dict[str, Callable[[Depth], None]] = {}
        self._ws: Any | None = None
        self._closing = False
        self._state_lock = threading.RLock()
        self._connect_lock = threading.Lock()
        #: Keys already subscribed per mode (``full`` vs ``full_d30``).
        #: Keyed by mode so a depth-30 subscription for the same instruments
        #: as a quote subscription is not suppressed by the quote-mode dedupe.
        self._subscribed_keys: dict[str, list[str]] = {}
        #: Canonical live instrument set (replayed on reconnect) and
        #: per-(instrument, mode) reference counts so ``unsubscribe`` prunes
        #: keys whose last holder released them instead of letting ghosts
        #: accumulate forever. ``_sub_instruments`` maps a subscription to the
        #: modes it holds (``{sub_id: {mode: {instrument, ...}}}``) so
        #: releasing one sub never tears down another sub's mode. Keyed by
        #: ``InstrumentId`` — ``Instrument`` is not hashable.
        self._subscribed_instruments: list[Instrument] = []
        self._sub_instruments: dict[str, dict[str, set[InstrumentId]]] = {}
        self._instrument_refs: dict[tuple[InstrumentId, str], int] = {}
        self._instrument_cache: dict[str, Instrument] = {}
        self._init_reconnect(reconnect=reconnect)

    def subscribe_quotes(
        self, instruments: Sequence[Instrument], handler: Callable[[Quote], None]
    ) -> str:
        subscription_id = f"upstox-quote-{id(handler)}"
        with self._state_lock:
            if self._closing:
                raise SessionStateError("Upstox market-data stream is closed")
            self._quote_handlers[subscription_id] = handler
            self._record_subscription(subscription_id, instruments, mode="full")
        self._ensure_ws()
        self._send_subscribes(instruments)
        return subscription_id

    def subscribe_depth(
        self, instruments: Sequence[Instrument], handler: Callable[[Depth], None]
    ) -> str:
        subscription_id = f"upstox-depth-{id(handler)}"
        with self._state_lock:
            if self._closing:
                raise SessionStateError("Upstox market-data stream is closed")
            self._depth_handlers[subscription_id] = handler
            self._record_subscription(subscription_id, instruments, mode="full")
        self._ensure_ws()
        self._send_subscribes(instruments)
        return subscription_id

    def subscribe_depth_30(
        self, instruments: Sequence[Instrument], handler: Callable[[Depth], None]
    ) -> str:
        """Subscribe to 30-level market depth via ``full_d30`` mode."""
        subscription_id = f"upstox-depth30-{id(handler)}"
        with self._state_lock:
            if self._closing:
                raise SessionStateError("Upstox market-data stream is closed")
            self._depth_handlers[subscription_id] = handler
            self._record_subscription(subscription_id, instruments, mode="full_d30")
        self._ensure_ws()
        self._send_subscribes(instruments, mode="full_d30")
        return subscription_id

    def _record_subscription(
        self, subscription_id: str, instruments: Sequence[Instrument], *, mode: str
    ) -> None:
        """Refcount *instruments* for *subscription_id* under the given mode."""
        by_mode = self._sub_instruments.setdefault(subscription_id, {})
        known = by_mode.setdefault(mode, set())
        for inst in instruments:
            iid = inst.instrument_id
            if iid in known:
                continue
            known.add(iid)
            self._instrument_refs[(iid, mode)] = (
                self._instrument_refs.get((iid, mode), 0) + 1
            )
            if inst not in self._subscribed_instruments:
                self._subscribed_instruments.append(inst)

    def unsubscribe(self, subscription: str) -> None:
        with self._state_lock:
            self._quote_handlers.pop(subscription, None)
            self._depth_handlers.pop(subscription, None)
            by_mode = self._sub_instruments.pop(subscription, {})
            removed: dict[str, list[str]] = {}
            for mode, instruments in by_mode.items():
                for mode_key, keys in self._drop_instruments(
                    instruments, modes=(mode,)
                ).items():
                    removed.setdefault(mode_key, []).extend(keys)
        self._send_unsub_frames(removed)

    def unsubscribe_instruments(self, instruments: Sequence[Instrument]) -> None:
        """Drop *instruments* from the live wire set (partial unsubscribe)."""
        with self._state_lock:
            removed = self._drop_instruments(instruments)
        self._send_unsub_frames(removed)

    def _drop_instruments(
        self,
        instruments: Iterable[Instrument | InstrumentId],
        *,
        modes: tuple[str, ...] = ("full", "full_d30"),
    ) -> dict[str, list[str]]:
        """Decrement per-mode refs and collect keys released by their last holder.

        Only the given *modes* are decremented: releasing a depth-30 sub must
        never tear down a quote-mode subscription the instrument still holds.
        """
        removed: dict[str, list[str]] = {}
        for inst in instruments:
            # Instruments carry their id; bare ids pass through unchanged.
            iid = cast(InstrumentId, getattr(inst, "instrument_id", inst))
            key = self._registry.provider_key(iid)
            key_str = str(key) if key is not None else None
            for mode in modes:
                ref_key = (iid, mode)
                refs = self._instrument_refs.get(ref_key, 0)
                if refs <= 1:
                    self._instrument_refs.pop(ref_key, None)
                    if key_str is not None:
                        mode_keys = self._subscribed_keys.get(mode)
                        if mode_keys and key_str in mode_keys:
                            mode_keys.remove(key_str)
                            removed.setdefault(mode, []).append(key_str)
                        if mode_keys is not None and not mode_keys:
                            self._subscribed_keys.pop(mode, None)
                else:
                    self._instrument_refs[ref_key] = refs - 1
            if all(
                self._instrument_refs.get((iid, mode), 0) == 0
                for mode in ("full", "full_d30")
            ):
                if any(i.instrument_id == iid for i in self._subscribed_instruments):
                    self._subscribed_instruments = [
                        i for i in self._subscribed_instruments if i.instrument_id != iid
                    ]
        return removed

    def _send_unsub_frames(self, removed: dict[str, list[str]]) -> None:
        """Send per-mode ``unsub`` frames for released keys (socket live only)."""
        if not removed:
            return
        ws = self._ws
        if ws is None or not hasattr(ws, "send"):
            return  # keys already pruned; a reconnect replays the live set
        for mode, keys in removed.items():
            if not keys:
                continue
            ws.send(
                json.dumps(
                    {
                        "guid": "tx",
                        "method": "unsub",
                        "data": {"mode": mode, "instrumentKeys": list(keys)},
                    }
                ).encode("utf-8")
            )

    def close(self) -> None:
        with self._state_lock:
            self._closing = True
            ws = self._ws
            self._ws = None
        if ws is not None and hasattr(ws, "close"):
            ws.close()

    def _authorize_ws_url(self) -> str:
        token = self._token_provider()
        status, body = self._ws_fetch(
            "GET",
            self._authorize_url,
            headers={"Authorization": f"Bearer {token}", "Accept": "application/json"},
        )
        # 401/403 — token was rejected. Notify the token manager (if it
        # supports ``rejected_token``) and retry once with a fresh token.
        if status in (401, 403):
            try:
                # Providers may be rejected-token-aware (``get_token_with_rejection``
                # style); plain factories raise TypeError, which is caught below.
                token = self._token_provider(rejected_token=token)  # type: ignore[call-arg]
            except TypeError:
                # Token provider doesn't support rejected_token — no retry.
                pass
            else:
                status, body = self._ws_fetch(
                    "GET",
                    self._authorize_url,
                    headers={"Authorization": f"Bearer {token}", "Accept": "application/json"},
                )
        data = body.get("data") if isinstance(body, dict) else None
        url: object = None
        if isinstance(data, dict):
            url = data.get("authorized_redirect_uri") or data.get("authorizedRedirectUri")
        if status != 200 or not isinstance(url, str) or not url:
            raise BrokerUnavailableError(
                f"Upstox market-data stream authorization failed (HTTP {status})"
            )
        return url

    def _open_socket(self) -> Any:
        """Authorize and open a new market socket (used on reconnect)."""
        return self._ws_factory(self._authorize_ws_url())

    def _resubscribe(self, ws: Any) -> None:
        """Replay the recorded per-mode subscription keys on a fresh socket.

        Keys are recorded eagerly in ``_send_subscribes`` (even while the
        socket is down), so instruments subscribed during the reconnect window
        are replayed here too.
        """
        if not hasattr(ws, "send"):
            return
        # Snapshot under the state lock — subscribe/unsubscribe threads mutate
        # the key sets concurrently with the reconnect worker.
        with self._state_lock:
            snapshot = {
                mode: list(keys) for mode, keys in self._subscribed_keys.items() if keys
            }
        for mode, keys in snapshot.items():
            ws.send(
                json.dumps(
                    {
                        "guid": "tx",
                        "method": "sub",
                        "data": {"mode": mode, "instrumentKeys": keys},
                    }
                ).encode("utf-8")
            )

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
                logger.warning("stream reopen resubscribe failed", exc_info=True)
            if hasattr(ws, "recv"):
                threading.Thread(target=self._receive_loop, daemon=True).start()

    def _send_subscribes(
        self, instruments: Sequence[Instrument], mode: str = "full"
    ) -> None:
        # Record keys eagerly so a subscription made while the socket is down
        # (e.g. during a reconnect window) is replayed once a socket exists.
        seen = self._subscribed_keys.setdefault(mode, [])
        new_keys: list[str] = []
        for instrument in instruments:
            key = self._registry.provider_key(instrument.instrument_id)
            if key is None:
                continue
            key_str = str(key)
            if key_str in seen:
                continue
            seen.append(key_str)
            new_keys.append(key_str)
        if not new_keys:
            return
        ws = self._ws
        if ws is None or not hasattr(ws, "send"):
            return  # recorded; replayed by _resubscribe on reconnect
        ws.send(
            json.dumps(
                {
                    "guid": "tx",
                    "method": "sub",
                    "data": {"mode": mode, "instrumentKeys": new_keys},
                }
            ).encode("utf-8")
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
                    logger.warning("upstox_market_ws_recv_failed", exc_info=True)
                    self._schedule_reconnect()
                return
            try:
                if isinstance(raw, (bytes, bytearray)):
                    self.feed_raw(bytes(raw))
            except Exception:  # noqa: BLE001
                logger.warning("upstox_market_frame_dispatch_failed", exc_info=True)

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
        """Decode one binary protobuf frame and dispatch Quote/Depth callbacks."""
        from tradex_brokers.upstox.ws_decoder import parse_feed_response

        rows = parse_feed_response(raw)
        for key, row in rows.items():
            instrument = self._cached_instrument(key)
            if instrument is None:
                continue
            quote = row_to_quote(instrument, row, provider="upstox")
            for handler in tuple(self._quote_handlers.values()):
                try:
                    handler(quote)
                except Exception:  # noqa: BLE001
                    logger.warning("stream_handler_failed", exc_info=True)
            if quote.depth is not None:
                for depth_handler in tuple(self._depth_handlers.values()):
                    try:
                        depth_handler(quote.depth)
                    except Exception:  # noqa: BLE001
                        logger.warning("stream_handler_failed", exc_info=True)


__all__ = [
    "UPSTOX_MARKET_DATA_AUTHORIZE_PATH",
    "UPSTOX_PORTFOLIO_AUTHORIZE_PATH",
    "UpstoxMarketDataStreamBackend",
    "UpstoxPortfolioStreamBackend",
    "default_ws_factory",
]
