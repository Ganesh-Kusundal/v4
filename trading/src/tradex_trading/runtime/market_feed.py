"""MarketFeed — bridge live broker WebSocket ticks into the reactive bus.

The live brokers expose ``subscribe_quotes`` (both) plus either
``subscribe_depth`` (Dhan's dedicated depth-20 backend) or
``subscribe_depth_30`` (Upstox 30-level mode). This bridge subscribes both
streams for a set of instruments and publishes every received ``Quote`` /
``Depth`` onto the session reactive bus, where stream consumers and
the FastAPI ``/ws/stream`` bridge pick them up.

Wired by ``TradingSession.live()``; paper sessions have no live feed.

Design
------
* ``MarketFeed`` keeps a single broker handler per stream type and a wanted
  instrument set; the handlers publish only ticks for wanted instruments.
  Instruments can be added (``subscribe``) or removed (``unsubscribe``) on the
  fly; depth can be toggled per ``subscribe`` call. ``start`` is a reset
  (stop + subscribe).  * ``FeedRegistry`` refcounts instrument subscriptions across multiple
    WebSocket clients sharing the session feed, so one client unsubscribing
    never drops a stream another client still holds. ``acquire``/``release``
    track client connections; teardown is owned by the refcounts and
    ``session.stop()``, never by a client release.
* Streaming capabilities come from the broker's ``BrokerCapabilities`` table
  (``depth_levels``, ``max_stream_instruments``): the feed advertises them
  and enforces the per-connection instrument cap loudly (``ValueError``)
  instead of sending more keys than the broker accepts.

Notes
-----
* Ticks arrive on each backend's daemon receive thread and are published
  straight into the bus — the same accepted cross-thread parity as the
  execution engine publishing ``OrderFilled`` from worker threads.
* Socket drops are healed by the broker backends themselves
  (``AutoReconnectMixin`` — reopen with a fresh token, replay the live
  subscription set, exponential backoff), not by the feed.
* ``stop()`` unsubscribes the broker handlers; the underlying WebSocket
  sockets are owned by the broker and torn down by ``broker.close()``
  (e.g. ``RuntimeContext.close``), not by the feed or session.
"""

from __future__ import annotations

import logging
import threading
from collections.abc import Sequence
from datetime import UTC, datetime
from typing import Any

from tradex_domain.events import StaleFeed
from tradex_domain.instruments import Instrument
from tradex_domain.market import Depth, Quote, require_depth_supported
from tradex_domain.value_objects import InstrumentId

log = logging.getLogger(__name__)


_DEPTH_MODES = {"off", "20", "30"}


def _depth_wanted(depth: object) -> bool:
    return str(depth or "").strip().lower() not in ("", "off")


def normalize_depth(depth: object) -> str:
    """Normalize a depth request to one of ``off``/``20``/``30``.

    Single source of truth for depth-mode normalization, shared by
    ``MarketFeed``/``FeedRegistry`` and the FastAPI ``/ws/stream`` bridge.
    """
    value = str(depth or "").strip().lower()
    return value if value in _DEPTH_MODES else "off"


class MarketFeed:
    """Live broker tick bridge: broker WebSockets -> reactive bus.

    Tracks a wanted instrument set and publishes ``Quote`` / ``Depth`` ticks
    for those instruments onto the session bus. Supports on-the-fly
    subscribe/unsubscribe and depth toggling.

    The requested depth level is advisory: the feed subscribes the deepest
    stream the broker supports (Upstox ``subscribe_depth_30``, else Dhan's
    depth-20 backend) and delivered frames carry a ``levels`` count the
    client can inspect. Level choice is bounded by broker capability.
    """

    def __init__(
        self, broker: Any, bus: Any, capabilities: Any | None = None, stale_after: float = 30.0
    ) -> None:
        self._broker = broker
        self._bus = bus
        self._instruments: dict[InstrumentId, Instrument] = {}
        self._depth_instruments: set[InstrumentId] = set()
        # H2: single RLock protecting _instruments and _depth_instruments.
        # RLock (re-entrant) so _on_quote can read while a dispatcher
        # is iterating the same dict under the lock.
        self._stream_lock = threading.RLock()
        self._quote_sub: object | None = None
        self._depth_sub: object | None = None
        self._depth_fn: Any | None = None
        self._depth_on = False
        self._last_tick: dict[InstrumentId, datetime] = {}
        self._last_tick_lock = threading.Lock()
        self._stale_after = float(stale_after)
        # Streaming capability surface: the broker's declared depth level and
        # per-connection instrument cap. Only a real BrokerCapabilities table
        # is trusted (duck-typed fakes/mocks expose none); backends are still
        # selected by getattr probing in ``_depth_method``.
        from tradex_domain.capabilities import BrokerCapabilities

        caps = capabilities if isinstance(capabilities, BrokerCapabilities) else None
        if caps is None:
            broker_caps = getattr(broker, "capabilities", None)
            if isinstance(broker_caps, BrokerCapabilities):
                caps = broker_caps
        self._depth_levels = int(caps.depth_levels) if caps is not None else 0
        raw_cap = caps.max_stream_instruments if caps is not None else None
        self._max_stream_instruments = int(raw_cap) if raw_cap else None
        # Bind the dispatch callbacks ONCE. Broker backends key handlers by
        # ``id(handler)``; a fresh ``self._on_quote`` bound method per call
        # would register a distinct handler every subscribe, fanning each tick
        # out N times. Stable callables make the backend overwrite the same
        # entry, keeping exactly one handler per stream on the shared socket.
        self._quote_cb = self._on_quote
        self._depth_cb = self._on_depth

    @property
    def depth_levels(self) -> int:
        """Declared depth levels of the broker's deepest depth stream."""
        return self._depth_levels

    @property
    def max_stream_instruments(self) -> int | None:
        """Max instruments per broker WebSocket connection (None = no cap)."""
        return self._max_stream_instruments

    def _check_cap(self, instruments: Sequence[Instrument]) -> None:
        """Raise ``ValueError`` when *instruments* would exceed the broker cap.

        Fails loudly instead of sending more keys than the broker accepts and
        silently dropping the excess.
        """
        cap = self._max_stream_instruments
        if cap is None:
            return
        projected = len(self._instruments) + sum(
            1 for inst in instruments if inst.instrument_id not in self._instruments
        )
        if projected > cap:
            raise ValueError(
                f"stream instrument cap exceeded: {projected} > {cap} "
                f"(max_stream_instruments)"
            )

    @property
    def active(self) -> bool:
        """Whether at least one instrument is wanted and not yet stopped."""
        return bool(self._instruments)

    @property
    def instruments(self) -> frozenset[InstrumentId]:
        """Instrument ids currently wanted by this feed.

        H2: read is guarded by ``_stream_lock`` so concurrent
        subscribe/unsubscribe cannot corrupt the iteration.
        """
        with self._stream_lock:
            return frozenset(self._instruments)

    def snapshot_instruments(self) -> tuple[Instrument, ...]:
        """Snapshot of wanted instruments under the stream lock (H2)."""
        with self._stream_lock:
            return tuple(self._instruments.values())

    @property
    def depth_enabled(self) -> bool:
        """Whether a depth subscription is currently active."""
        return self._depth_on

    @property
    def subscription_count(self) -> int:
        """Number of active broker stream subscriptions (0-2)."""
        return int(self._quote_sub is not None) + int(self._depth_sub is not None)

    def start(self, instruments: Sequence[Instrument], *, depth: object = "off") -> None:
        """Reset the feed to exactly *instruments* (and optional depth)."""
        self.stop()
        self.subscribe(instruments, depth=depth)

    def subscribe(
        self,
        instruments: Sequence[Instrument],
        *,
        depth: object = "off",
    ) -> None:
        """Add *instruments* to the wanted set; enable depth if requested.

        Idempotent per instrument. A ``depth`` value other than ``"off"``
        subscribes the depth stream for *every requested instrument* —
        including instruments already wanted quote-only — so toggling depth
        on for an existing instrument works even when depth is already active
        for other instruments.
        """
        want_depth = _depth_wanted(depth)
        if want_depth:
            for inst in instruments:
                require_depth_supported(inst)
        self._check_cap(instruments)
        with self._stream_lock:  # H2
            added = [
                inst for inst in instruments
                if inst.instrument_id not in self._instruments
            ]
            if added:
                try:
                    sub = self._broker.subscribe_quotes(added, self._quote_cb)
                except Exception:
                    raise
                else:
                    for inst in added:
                        self._instruments[inst.instrument_id] = inst
                    self._quote_sub = sub
        if want_depth:
            with self._stream_lock:  # H2
                missing = [
                    inst
                    for inst in instruments
                    if inst.instrument_id not in self._depth_instruments
                ]
                if missing:
                    try:
                        self._ensure_depth(missing)
                    except Exception:
                        raise

    def unsubscribe(self, instruments: Sequence[Instrument]) -> None:
        """Remove *instruments* from the wanted set.

        The broker backends are told to drop the instruments from their live
        wire set (``unsubscribe_instruments``), so reconnects never resurrect
        them and the wire set stops growing toward the broker's per-connection
        cap. Handlers stay registered and filter out removed instruments; when
        the wanted set empties, the feed stops.
        """
        with self._stream_lock:  # H2
            removed = False
            removed_instruments: list[Instrument] = []
            for inst in instruments:
                iid = inst.instrument_id
                if iid in self._instruments:
                    del self._instruments[iid]
                    removed = True
                    removed_instruments.append(inst)
                self._depth_instruments.discard(iid)
        if removed_instruments:
            drop = getattr(self._broker, "unsubscribe_instruments", None)
            if callable(drop):
                try:
                    drop(removed_instruments)
                except Exception:  # noqa: BLE001 — best-effort wire cleanup
                    log.warning(
                        "market feed unsubscribe_instruments failed", exc_info=True
                    )
        # Re-check the wanted set under the lock for the empty-feed stop
        # decision; the earlier read might have raced with a concurrent
        # subscribe.
        with self._stream_lock:  # H2
            if removed and not self._instruments:
                self.stop()

    def stop(self) -> None:
        """Unsubscribe every broker stream and clear the wanted set."""
        for sub in (self._quote_sub, self._depth_sub):
            if sub is not None:
                try:
                    self._broker.unsubscribe(sub)
                except Exception:  # noqa: BLE001 — defensive teardown
                    log.warning("market feed unsubscribe failed", exc_info=True)
        self._quote_sub = None
        self._depth_sub = None
        self._depth_on = False
        self._instruments.clear()
        self._depth_instruments.clear()

    def stop_depth(self) -> None:
        """Tear down the depth stream (quotes keep streaming)."""
        if self._depth_sub is not None:
            try:
                self._broker.unsubscribe(self._depth_sub)
            except Exception:  # noqa: BLE001 — defensive teardown
                log.warning("market feed depth unsubscribe failed", exc_info=True)
        self._depth_sub = None
        self._depth_on = False
        self._depth_instruments.clear()

    # -- depth helpers ------------------------------------------------------

    def _depth_method(self) -> Any | None:
        if self._depth_fn is None:
            fn = getattr(self._broker, "subscribe_depth_30", None)
            if fn is None:
                fn = getattr(self._broker, "subscribe_depth", None)
            self._depth_fn = fn
        return self._depth_fn

    def _ensure_depth(self, instruments: Sequence[Instrument]) -> None:
        fn = self._depth_method()
        if fn is None:
            return
        self._depth_sub = fn(instruments, self._depth_cb)
        self._depth_on = self._depth_sub is not None
        if self._depth_on:
            self._depth_instruments.update(i.instrument_id for i in instruments)

    # -- handlers -----------------------------------------------------------

    def _on_quote(self, quote: Quote) -> None:
        if quote.instrument.instrument_id in self._instruments:
            self._bus.publish(quote)
            with self._last_tick_lock:
                self._last_tick[quote.instrument.instrument_id] = datetime.now(UTC)

    def _on_depth(self, depth: Depth) -> None:
        if depth.instrument.instrument_id in self._instruments:
            self._bus.publish(depth)

    def check_stale(self) -> list[StaleFeed]:
        """Emit StaleFeed for instruments with no tick for longer than stale_after.

        Poll-driven: call ``check_stale()`` periodically (metrics tick or
        strategy watchdog). No background Timer — future: add Timer if needed.
        Thread-safe via ``_last_tick_lock``.
        """
        now = datetime.now(UTC)
        stale: list[StaleFeed] = []
        with self._last_tick_lock:
            snapshot = list(self._last_tick.items())
        for iid, last in snapshot:
            inst = self._instruments.get(iid)
            if inst is None:
                continue
            age = (now - last).total_seconds()
            if age > self._stale_after:
                evt = StaleFeed(instrument=inst, age_seconds=age, last_timestamp=last)
                stale.append(evt)
                self._bus.publish(evt)
        return stale


class FeedRegistry:
    """Refcount instrument subscriptions across multiple WebSocket clients.

    Multiple clients share one session ``MarketFeed``. ``subscribe`` /
    ``unsubscribe`` track per-instrument reference counts so a stream stays
    live while any client holds it and is only dropped when the last client
    releases it. Depth is enabled when any client requests it.
    """

    def __init__(self, feed: MarketFeed | None = None) -> None:
        self._feed = feed
        self._counts: dict[InstrumentId, int] = {}
        self._depth_counts: dict[InstrumentId, int] = {}
        self._connections = 0
        self._lock = threading.Lock()

    @property
    def feed(self) -> MarketFeed | None:
        return self._feed

    @property
    def connection_count(self) -> int:
        """Number of WebSocket clients currently holding this registry."""
        return self._connections

    @property
    def any_depth(self) -> bool:
        """Whether any client currently holds a depth subscription."""
        return any(self._depth_counts.values())

    def acquire(self) -> None:
        """Register a client connection sharing this feed."""
        self._connections += 1

    def release(self) -> None:
        """Drop a client connection (pure connection accounting).

        Feed teardown is owned by the per-instrument refcounts (the last
        holder's ``unsubscribe`` stops the feed) and by ``session.stop()`` —
        releasing here must never yank a feed the app itself started via
        ``session.start_market_feed`` for an in-process consumer.
        """
        self._connections = max(0, self._connections - 1)

    def subscribe(
        self,
        instruments: Sequence[Instrument],
        *,
        depth: object = "off",
    ) -> None:
        """Add refs for *instruments*; new instruments join the feed stream."""
        want_depth = _depth_wanted(depth)
        if want_depth:
            for inst in instruments:
                require_depth_supported(inst)
        with self._lock:
            if self._feed is not None:
                # Pre-check the broker's per-connection cap before mutating refs,
                # so an oversized request leaves the registry untouched.
                cap = self._feed.max_stream_instruments
                if isinstance(cap, int):
                    projected = len(self._counts) + sum(
                        1 for inst in instruments if inst.instrument_id not in self._counts
                    )
                    if projected > cap:
                        raise ValueError(
                            f"stream instrument cap exceeded: {projected} > {cap} "
                            f"(max_stream_instruments)"
                        )
            with_depth: list[Instrument] = []
            quote_only: list[Instrument] = []
            for inst in instruments:
                iid = inst.instrument_id
                self._counts[iid] = self._counts.get(iid, 0) + 1
                if want_depth:
                    self._depth_counts[iid] = self._depth_counts.get(iid, 0) + 1
                    with_depth.append(inst)
                else:
                    quote_only.append(inst)
        if self._feed is not None:
            # Quote-only instruments join without pulling depth into the feed;
            # depth-requested instruments always enable depth for themselves.
            if quote_only:
                self._feed.subscribe(quote_only)
            if with_depth:
                self._feed.subscribe(with_depth, depth="30")

    def unsubscribe(self, instruments: Sequence[Instrument]) -> None:
        """Drop refs for *instruments*; last holder removes them from the feed."""
        with self._lock:
            dropped: list[Instrument] = []
            for inst in instruments:
                iid = inst.instrument_id
                remaining = self._counts.get(iid, 0) - 1
                if remaining <= 0:
                    self._counts.pop(iid, None)
                    dropped.append(inst)
                else:
                    self._counts[iid] = remaining
                depth_remaining = self._depth_counts.get(iid, 0) - 1
                if depth_remaining <= 0:
                    self._depth_counts.pop(iid, None)
                else:
                    self._depth_counts[iid] = depth_remaining
            any_depth = any(self._depth_counts.values())
        if self._feed is not None and dropped:
            self._feed.unsubscribe(dropped)
        # Tear down the depth stream once the last depth client is gone, so a
        # later quote-only subscription never gets a depth socket opened.
        if self._feed is not None and not any_depth and self._feed.depth_enabled:
            self._feed.stop_depth()


__all__ = ["FeedRegistry", "MarketFeed", "StaleFeed", "normalize_depth"]
