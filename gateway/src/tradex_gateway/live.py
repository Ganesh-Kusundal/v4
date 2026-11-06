"""Live feed hub: refcounted broker subscriptions, fan-out and cold failover."""

from __future__ import annotations

import asyncio
import logging
import threading
import time
import uuid
from collections.abc import Callable, Mapping
from dataclasses import dataclass, field
from datetime import UTC, datetime
from typing import Any, cast

from tradex_domain.instruments import Instrument
from tradex_domain.market import Depth, Quote

from tradex_gateway.brokers import BrokerRuntime
from tradex_gateway.config import GatewayConfig
from tradex_gateway.directory import InstrumentDirectory
from tradex_gateway.market import CachedDepth, depth_payload, quote_payload
from tradex_gateway.router import MarketDataRouter

log = logging.getLogger(__name__)

_TICK_QUEUE_MAX = 4096
_CONTROL_QUEUE_MAX = 256


@dataclass
class _Client:
    client_id: str
    loop: asyncio.AbstractEventLoop
    queue: asyncio.Queue
    control: asyncio.Queue
    instruments: dict[str, Instrument] = field(default_factory=dict)
    subscriptions: dict[str, tuple[str, int]] = field(default_factory=dict)
    subscription_map: dict[str, list[str]] = field(default_factory=dict)
    close_requested: bool = False


class _BrokerFeed:
    """One broker's upstream subscriptions for the current wanted sets."""

    def __init__(self, runtime: BrokerRuntime, hub: LiveHub) -> None:
        self.runtime = runtime
        self.name = runtime.name
        broker = runtime.broker
        if broker is None:
            raise ValueError(f"broker runtime {runtime.name} has no adapter")
        self.broker = cast(Any, broker)
        self.hub = hub
        self._lock = threading.RLock()
        self._quotes: dict[str, Instrument] = {}
        self._depths: dict[str, Instrument] = {}
        self._levels = 0
        self._quote_sub: Any | None = None
        self._depth_sub: Any | None = None
        # Bind once: broker backends key handlers by id(handler), and a fresh
        # bound method per subscribe would register a distinct handler.
        self._quote_cb = self._on_quote
        self._depth_cb = self._on_depth

    def sync_quotes(self, instruments: Mapping[str, Instrument]) -> None:
        with self._lock:
            add = [inst for iid, inst in instruments.items() if iid not in self._quotes]
            remove = [inst for iid, inst in self._quotes.items() if iid not in instruments]
            if add:
                if self._quote_sub is None:
                    self._quote_sub = self.broker.subscribe_quotes(add, self._quote_cb)
                else:
                    self.broker.subscribe_quotes(add, self._quote_cb)
            if remove:
                self.broker.unsubscribe_instruments(remove)
            self._quotes = dict(instruments)

    def sync_depth(self, instruments: Mapping[str, Instrument], levels: int) -> None:
        with self._lock:
            if levels == self._levels and set(instruments) == set(self._depths):
                return
            if self._depth_sub is not None:
                self._stop_depth()
            if instruments:
                self._depth_sub = self._subscribe_depth(list(instruments.values()), levels)
                self._depths = dict(instruments)
                self._levels = levels

    def _subscribe_depth(self, instruments: list[Instrument], levels: int) -> Any:
        declared = int(getattr(self.runtime.capabilities, "depth_levels", 0) or 0)
        deep = getattr(self.broker, "subscribe_depth_30", None)
        if levels > 5 and declared >= 30 and callable(deep):
            return deep(instruments, self._depth_cb)
        return self.broker.subscribe_depth(instruments, self._depth_cb)

    def _stop_depth(self) -> None:
        if self._depth_sub is not None:
            try:
                self.broker.unsubscribe(self._depth_sub)
            except Exception:  # noqa: BLE001 - teardown is best effort
                pass
            self._depth_sub = None
        if self._depths:
            try:
                self.broker.unsubscribe_instruments(list(self._depths.values()))
            except Exception:  # noqa: BLE001 - teardown is best effort
                pass
        self._depths = {}
        self._levels = 0

    def _on_quote(self, quote: Quote) -> None:
        self.hub._publish_quote(quote, self.name)

    def _on_depth(self, depth: Depth) -> None:
        self.hub._publish_depth(depth, self.name)

    def stop(self) -> None:
        with self._lock:
            instruments = list(self._quotes.values()) + list(self._depths.values())
            if instruments:
                try:
                    self.broker.unsubscribe_instruments(instruments)
                except Exception:  # noqa: BLE001 - teardown is best effort
                    pass
            for subscription in (self._quote_sub, self._depth_sub):
                if subscription is not None:
                    try:
                        self.broker.unsubscribe(subscription)
                    except Exception:  # noqa: BLE001 - teardown is best effort
                        pass
            self._quote_sub = None
            self._depth_sub = None
            self._quotes = {}
            self._depths = {}
            self._levels = 0


class LiveHub:
    def __init__(
        self,
        config: GatewayConfig,
        router: MarketDataRouter,
        directory: InstrumentDirectory,
        runtimes: Mapping[str, BrokerRuntime],
        *,
        clock: Callable[[], float] = time.monotonic,
        on_event: Callable[[str], None] | None = None,
    ) -> None:
        self._config = config
        self._router = router
        self._directory = directory
        self._runtimes = dict(runtimes)
        self._clock = clock
        self._event = on_event or (lambda _name: None)
        self._clients: dict[str, _Client] = {}
        self._active: str | None = None
        self._feed: _BrokerFeed | None = None
        self._last_tick: dict[str, float] = {}
        self._last_depth: dict[str, CachedDepth] = {}
        # Serializes switches and teardown across threads; it may span broker
        # calls. The state lock `_lock` is the one never held across broker I/O.
        self._switch_lock = threading.RLock()
        self._lock = threading.RLock()
        self._stop = threading.Event()
        self._monitor: threading.Thread | None = None
        self._next_switch_at = 0.0
        self._stale_snapshot: set[str] = set()

    # -- client lifecycle -------------------------------------------------
    def add_client(
        self,
        client_id: str,
        loop: asyncio.AbstractEventLoop,
        queue: asyncio.Queue,
        control: asyncio.Queue,
    ) -> None:
        with self._lock:
            self._clients[client_id] = _Client(client_id, loop, queue, control)

    def remove_client(self, client_id: str) -> None:
        with self._lock:
            self._clients.pop(client_id, None)
        self._rebalance()

    def subscribe(
        self,
        client_id: str,
        instrument_ids: list[str],
        mode: str,
        levels: int,
    ) -> dict[str, Any]:
        if len(instrument_ids) > self._config.max_subscriptions:
            return _error("too_many_subscriptions", "subscription limit exceeded")
        if mode not in {"quote", "depth"}:
            return _error("invalid_request", f"invalid mode: {mode!r}")
        accepted: list[str] = []
        errors: dict[str, str] = {}
        resolved: dict[str, Instrument] = {}
        for raw in instrument_ids:
            try:
                item = self._directory.resolve(raw)
            except Exception as exc:  # noqa: BLE001 - one bad ID must not fail the rest
                errors[raw] = str(exc)
                continue
            key = str(item.instrument_id)
            resolved[key] = item.instrument
            accepted.append(key)
        with self._lock:
            client = self._clients.get(client_id)
            if client is None:
                return _error("unknown_client", "client is not registered")
            added = [key for key in accepted if key not in client.subscriptions]
            if len(client.subscriptions) + len(added) > self._config.max_subscriptions:
                return _error("too_many_subscriptions", "subscription limit exceeded")
            client.instruments.update(resolved)
            for key in accepted:
                client.subscriptions[key] = (mode, max(1, min(30, levels)))
            subscription_id = uuid.uuid4().hex
            client.subscription_map[subscription_id] = list(accepted)
        broker = self._rebalance()
        return {
            "op": "ack",
            "subscription_id": subscription_id,
            "instruments": accepted,
            "mode": mode,
            "levels": max(1, min(30, levels)),
            "broker": broker,
            "errors": errors,
        }

    def unsubscribe(
        self,
        client_id: str,
        instrument_ids: list[str] | None = None,
        subscription_id: str | None = None,
    ) -> dict[str, Any]:
        removed: list[str] = []
        with self._lock:
            client = self._clients.get(client_id)
            if client is None:
                return _error("unknown_client", "client is not registered")
            if subscription_id:
                targets = client.subscription_map.pop(subscription_id, [])
            elif instrument_ids:
                requested = set(instrument_ids)
                targets = [key for key in client.subscriptions if key in requested]
                for sid, keys in list(client.subscription_map.items()):
                    remaining = [key for key in keys if key not in requested]
                    if remaining:
                        client.subscription_map[sid] = remaining
                    else:
                        client.subscription_map.pop(sid, None)
            else:
                targets = list(client.subscriptions)
                client.subscription_map.clear()
            for key in targets:
                if any(key in keys for keys in client.subscription_map.values()):
                    continue
                client.subscriptions.pop(key, None)
                client.instruments.pop(key, None)
                removed.append(key)
        self._rebalance()
        return {"op": "ack", "unsubscribed": removed}

    def close_requested(self, client_id: str) -> bool:
        with self._lock:
            client = self._clients.get(client_id)
            return bool(client is not None and client.close_requested)

    # -- fan-out ----------------------------------------------------------
    def _publish_quote(self, quote: Quote, broker_name: str) -> None:
        instrument_id = str(quote.instrument.instrument_id)
        with self._lock:
            if broker_name != self._active:
                return
            self._last_tick[instrument_id] = self._clock()
            if quote.depth is not None:
                self._last_depth[instrument_id] = CachedDepth(
                    depth=quote.depth,
                    broker=broker_name,
                    received_at=datetime.now(UTC),
                )
            clients = list(self._clients.values())
        for client in clients:
            subscription = client.subscriptions.get(instrument_id)
            if subscription is None:
                continue
            mode, _ = subscription
            if mode == "quote":
                self._send(
                    client,
                    {"op": "quote", **quote_payload(quote, broker_name)},
                )

    def _publish_depth(self, depth: Depth, broker_name: str) -> None:
        instrument_id = str(depth.instrument.instrument_id)
        with self._lock:
            if broker_name != self._active:
                return
            self._last_tick[instrument_id] = self._clock()
            self._last_depth[instrument_id] = CachedDepth(
                depth=depth,
                broker=broker_name,
                received_at=datetime.now(UTC),
            )
            clients = list(self._clients.values())
        for client in clients:
            subscription = client.subscriptions.get(instrument_id)
            if subscription is None or subscription[0] != "depth":
                continue
            levels = subscription[1]
            payload = depth_payload(depth, broker_name, levels, source="stream", degraded=False)
            payload["op"] = "depth"
            payload["levels"] = payload["delivered_levels"]
            self._send(client, payload)

    def _send(self, client: _Client, frame: dict[str, Any]) -> None:
        peer = client.queue if frame.get("op") not in {"status", "gap"} else client.control

        def put() -> None:
            try:
                peer.put_nowait(frame)
            except asyncio.QueueFull:
                if peer is client.control:
                    client.close_requested = True
                    return
                try:
                    peer.get_nowait()
                except asyncio.QueueEmpty:
                    pass
                try:
                    peer.put_nowait(frame)
                except asyncio.QueueFull:
                    pass

        try:
            client.loop.call_soon_threadsafe(put)
        except RuntimeError:
            # The connection closed between the snapshot and this send.
            pass

    def _broadcast(self, frame: dict[str, Any]) -> None:
        with self._lock:
            clients = list(self._clients.values())
        for client in clients:
            self._send(client, frame)

    def latest_depth(self, instrument_id: str) -> CachedDepth | None:
        with self._lock:
            return self._last_depth.get(instrument_id)

    def status(self) -> dict[str, Any]:
        with self._lock:
            return {
                "state": "ready" if self._active else "halted",
                "broker": self._active,
                "clients": len(self._clients),
                "instruments": len(
                    {key for client in self._clients.values() for key in client.subscriptions}
                ),
            }

    # -- feed selection ---------------------------------------------------
    def _wanted(self) -> tuple[dict[str, Instrument], dict[str, Instrument], int]:
        quotes: dict[str, Instrument] = {}
        depths: dict[str, Instrument] = {}
        levels = 5
        with self._lock:
            for client in self._clients.values():
                for key, (mode, wanted_levels) in client.subscriptions.items():
                    instrument = client.instruments.get(key)
                    if instrument is None:
                        continue
                    if mode == "depth":
                        depths[key] = instrument
                        levels = max(levels, wanted_levels)
                    else:
                        quotes[key] = instrument
        return quotes, depths, levels

    def _prune_state(self, wanted: set[str]) -> None:
        with self._lock:
            for key in list(self._last_tick):
                if key not in wanted:
                    self._last_tick.pop(key, None)
            for key in list(self._last_depth):
                if key not in wanted:
                    self._last_depth.pop(key, None)

    def _choose_broker(self, depth_levels: int) -> tuple[str, int] | None:
        for runtime in self._router.order("live"):
            health = runtime.health
            if not health.available("live"):
                continue
            caps = runtime.capabilities
            declared = int(getattr(caps, "depth_levels", 0) or 0)
            if depth_levels > 5 and declared < min(depth_levels, 6):
                continue
            effective = min(depth_levels, declared) if depth_levels > 5 else 5
            return runtime.name, effective
        return None

    def _rebalance(self) -> str | None:
        quotes, depths, levels = self._wanted()
        self._prune_state(set(quotes) | set(depths))
        if not quotes and not depths:
            self._stop_feed()
            return None
        choice = self._choose_broker(levels if depths else 5)
        if choice is None:
            self._broadcast({"op": "status", "state": "halted", "broker": None})
            return None
        broker_name, effective_levels = choice
        if self._active != broker_name or self._feed is None:
            self._switch_to(broker_name, reason="failover" if self._active else "initial")
            return self._active
        feed = self._feed
        if feed is None:
            return None
        try:
            feed.sync_quotes(quotes)
            if depths:
                feed.sync_depth(depths, effective_levels)
            elif feed._depths:
                feed.sync_depth({}, 0)
        except Exception as exc:  # noqa: BLE001 - a subscribe failure is a failover trigger
            log.warning("live subscribe failed on %s: %s", broker_name, exc)
            self._runtimes[broker_name].health.record_failure("live", 15.0, str(exc))
            self._switch_to(self._other_broker(broker_name), reason="failover")
            return self._active
        self._ensure_monitor()
        return self._active

    def _other_broker(
        self, current: str | None, exclude: frozenset[str] = frozenset()
    ) -> str | None:
        for runtime in self._router.order("live"):
            if runtime.name != current and runtime.name not in exclude:
                return runtime.name
        return None

    def _switch_to(
        self,
        broker_name: str | None,
        *,
        reason: str,
        attempted: frozenset[str] = frozenset(),
    ) -> None:
        if broker_name is None or broker_name in attempted:
            self._active = None
            self._broadcast({"op": "status", "state": "halted", "broker": None})
            return
        attempted = attempted | {broker_name}
        self._event("live_switches_total")
        self._broadcast({"op": "status", "state": "switching", "broker": broker_name})
        with self._switch_lock:
            with self._lock:
                previous = self._active
                old_feed = self._feed
                quotes, depths, levels = self._wanted()
                self._feed = None
                self._active = None

            if old_feed is not None:
                old_feed.stop()  # I/O outside the state lock

            runtime = self._runtimes.get(broker_name)
            if runtime is None or runtime.broker is None:
                self._broadcast({"op": "status", "state": "halted", "broker": None})
                return

            feed = _BrokerFeed(runtime, self)
            effective = min(levels, int(getattr(runtime.capabilities, "depth_levels", 0) or 5))
            try:
                feed.sync_quotes(quotes)
                if depths:
                    feed.sync_depth(depths, effective)
            except Exception as exc:  # noqa: BLE001 - try the other broker
                runtime.health.record_failure("live", 15.0, str(exc))
                feed.stop()
                target = self._other_broker(broker_name, exclude=attempted)
                if target is not None:
                    self._switch_to(target, reason=reason, attempted=attempted)
                else:
                    self._broadcast({"op": "status", "state": "halted", "broker": None})
                return

            with self._lock:
                self._feed = feed
                self._active = broker_name

            self._broadcast({"op": "status", "state": "ready", "broker": broker_name})
            if previous is not None and previous != broker_name:
                for key in {*quotes, *depths}:
                    self._broadcast(
                        {
                            "op": "gap",
                            "instrument": key,
                            "from_ms": None,
                            "to_ms": int(time.time() * 1000),
                            "reason": reason,
                            "broker": broker_name,
                        }
                    )
            self._ensure_monitor()

    def _stop_feed(self) -> None:
        with self._switch_lock:
            with self._lock:
                feed = self._feed
                self._feed = None
                self._active = None
            if feed is not None:
                feed.stop()  # I/O outside the state lock

    # -- staleness monitor ------------------------------------------------
    def _ensure_monitor(self) -> None:
        with self._lock:
            if self._monitor is not None and self._monitor.is_alive():
                return
            self._monitor = threading.Thread(
                target=self._monitor_loop, name="gateway-live-monitor", daemon=True
            )
            self._monitor.start()

    def _stale_instruments(self, now: float, wanted: set[str]) -> list[str]:
        return sorted(
            key
            for key in wanted
            if now - self._last_tick.get(key, 0.0) > self._config.stale_after_seconds
        )

    def _monitor_cycle(self) -> None:
        quotes, depths, _ = self._wanted()
        wanted = set(quotes) | set(depths)
        if not wanted:
            return
        now = self._clock()
        stale = self._stale_instruments(now, wanted)
        if set(stale) != self._stale_snapshot:
            self._stale_snapshot = set(stale)
            if stale:
                self._broadcast(
                    {
                        "op": "status",
                        "state": "degraded",
                        "stale": stale,
                        "broker": self._active,
                    }
                )
        if len(stale) != len(wanted) or now < self._next_switch_at:
            return
        stale_broker = self._active
        if stale_broker is None:
            self._next_switch_at = now + 10.0
            self._rebalance()
            return
        self._runtimes[stale_broker].health.record_failure("live", 15.0, "stale feed")
        self._next_switch_at = now + 10.0
        target = self._other_broker(stale_broker)
        if target is not None:
            self._switch_to(target, reason="stale_feed")

    def _monitor_loop(self) -> None:
        interval = max(1.0, min(2.0, self._config.stale_after_seconds / 3))
        while not self._stop.wait(interval):
            try:
                self._monitor_cycle()
            except Exception:  # noqa: BLE001
                log.exception("live monitor iteration failed")

    def stop(self) -> None:
        self._stop.set()
        self._stop_feed()
        with self._lock:
            self._clients.clear()


def _error(code: str, message: str) -> dict[str, Any]:
    return {"op": "error", "code": code, "message": message}
