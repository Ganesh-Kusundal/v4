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
  (stop + subscribe) **and the cold boot's readiness decision**: it drives the
  fail-closed recovery coordinator and refuses when continuity is not proven.
  * ``FeedRegistry`` refcounts instrument subscriptions across multiple
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
* An optional ``recorder`` (a :class:`~tradex_runtime.tape_recorder.TapeRecorder`)
  writes every accepted quote to a JSONL tape, so a recorded session exists
  for the parity suite to compare against. It is ``None`` unless the
  ``TRADEX_RECORD_TAPE`` gate is on, and the recorder's owner closes it.
"""

from __future__ import annotations

import logging
import threading
from collections.abc import Sequence
from datetime import UTC, datetime
from typing import Any

from tradex_domain.constants import DEPTH_MODES, RECOVERY_TRIGGERS
from tradex_domain.errors import SDKError
from tradex_domain.events import FeedGapDetected, StaleFeed
from tradex_domain.instruments import Instrument
from tradex_domain.market import Depth, Quote, require_depth_supported
from tradex_domain.value_objects import InstrumentId
from tradex_observability.tracing import get_correlation_id

from tradex_runtime.feed_integrity import FeedIntegrityTracker
from tradex_runtime.feed_supervisor import FeedState, FeedSupervisor
from tradex_runtime.tape_recorder import TapeRecorder

log = logging.getLogger(__name__)

#: Reason recorded when a recovery attempt reported no cause of its own.
_UNPROVEN_CONTINUITY = "feed recovery did not prove tape continuity"


class FeedNotReadyError(SDKError):
    """A cold start could not prove feed continuity, so the feed refused to start.

    Raised by :meth:`MarketFeed.start` — and therefore by
    ``TradingSession.start_market_feed`` — when the history reconciliation that
    the cold boot path requires did not succeed. The feed is left not READY, so
    the order gate stays shut, and the message carries the coordinator's reason.
    """


def _depth_wanted(depth: object) -> bool:
    return str(depth or "").strip().lower() not in ("", "off")


def normalize_depth(depth: object) -> str:
    """Normalize a depth request to one of ``off``/``20``/``30``.

    Single source of truth for depth-mode normalization, shared by
    ``MarketFeed``/``FeedRegistry`` and the FastAPI ``/ws/stream`` bridge.
    """
    value = str(depth or "").strip().lower()
    return value if value in DEPTH_MODES else "off"


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
        self,
        broker: Any,
        bus: Any,
        capabilities: Any | None = None,
        stale_after: float = 30.0,
        supervisor: FeedSupervisor | None = None,
        integrity: FeedIntegrityTracker | None = None,
        recovery: Any | None = None,
        recorder: TapeRecorder | None = None,
    ) -> None:
        self._broker = broker
        self._bus = bus
        self._supervisor = supervisor or FeedSupervisor()
        self._integrity = integrity
        self._recorder = recorder
        self._recovery = recovery
        self._recovery_lock = threading.Lock()
        self._recovery_in_flight = False
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
        # Stable reference for the reconnect hook (B0). Brokers call this after
        # a successful socket reopen; we demote to RESYNCHRONIZING then kick the
        # history recovery coordinator. notify_reconnect alone still never runs
        # recovery (session.start / explicit API contract).
        self._reconnect_cb = self._on_broker_reconnect

    @property
    def supervisor(self) -> FeedSupervisor:
        """Feed health and recovery state for this market feed."""
        return self._supervisor

    @property
    def integrity(self) -> FeedIntegrityTracker | None:
        """Gap/duplicate accounting for this feed (``None`` when unwired)."""
        return self._integrity

    @property
    def stale_after(self) -> float:
        """Seconds without a tick after which the feed is considered stale."""
        return self._stale_after

    @property
    def depth_levels(self) -> int:
        """Declared depth levels of the broker's deepest depth stream."""
        return self._depth_levels

    @property
    def max_stream_instruments(self) -> int | None:
        """Max instruments per broker WebSocket connection (None = no cap)."""
        return self._max_stream_instruments

    def wanted_instruments(self) -> list[Instrument]:
        """A snapshot of every instrument the feed is subscribed to.

        The per-symbol cadence monitor (AC-12, lane 3d) needs the *wanted* set,
        not the tick set: a symbol the wire stops delivering for is absent from
        :meth:`check_stale`'s state rather than stale in it, so that is the one
        failure no amount of tape-level watching can see.
        """
        with self._stream_lock:
            return list(self._instruments.values())

    def last_tick(self, instrument: Instrument | None = None) -> datetime | None:
        """When *instrument* (or the most recent tick at all) last arrived.

        The cadence monitor ages a symbol from its own first tick, so this has
        to answer per instrument; ``None`` for an instrument that has never
        ticked, which the caller must distinguish from "ticked long ago" — the
        two get different verdicts, and collapsing them here would make a
        never-subscribed symbol look like a dead one.
        """
        iid = getattr(instrument, "instrument_id", instrument)
        with self._last_tick_lock:
            if instrument is None:
                return max(self._last_tick.values(), default=None)
            return self._last_tick.get(iid)

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

    def set_recovery(self, recovery: Any) -> None:
        """Wire the fail-closed reconnect recovery coordinator.

        Called by the composition root after both ``MarketFeed`` and
        ``FeedRecoveryCoordinator`` are constructed.  Thread-safe: the
        assignment is atomic on CPython and guarded by ``_recovery_lock`` in
        ``_trigger_recovery``.
        """
        self._recovery = recovery

    def notify_reconnect(self) -> None:
        """Signal that the broker socket just reconnected.

        Marks the supervisor ``CONNECTING`` → ``RESYNCHRONIZING`` so the feed
        is never READY on a bare socket reconnect.  This method never runs
        the coordinator itself — broker hooks use ``_on_broker_reconnect``.
        """
        self._supervisor.connected()
        self._supervisor.recovery_started()

    def _on_broker_reconnect(self) -> None:
        """Broker reconnect hook: demote readiness, then run fail-closed recovery."""
        self.notify_reconnect()
        self._trigger_recovery()

    def start(self, instruments: Sequence[Instrument], *, depth: object = "off") -> Any:
        """Cold boot: reset the feed to *instruments*, then prove it may trade.

        ``stop`` + ``subscribe`` alone leaves the supervisor in
        ``RESYNCHRONIZING``, which is terminal — nothing on this path ever
        reached ``READY``, so a session that started its feed this way rejected
        every order with ``feed_not_ready`` for the rest of its life. The cold
        start is the one moment the feed has to *become* ready, so it drives
        the same fail-closed coordinator a reconnect drives, and gates on the
        outcome rather than on the socket: a connected socket says nothing about
        the tape between the last bar this process saw and now, and the history
        reconciliation is what says it.

        Runs the recovery on this thread, so a caller can treat the return as
        the boot's verdict (it blocks on history I/O).

        Returns
        -------
        The coordinator's outcome when a coordinator is wired and recovery
        succeeded, else ``None``.

        Raises
        ------
        FeedNotReadyError
            If the recovery attempt failed. The feed is left not READY and the
            error names the reason, so the session refuses to start trading.
        """
        self.stop()
        self._supervisor.connected()
        self._supervisor.recovery_started()
        self.subscribe(instruments, depth=depth)
        return self._prove_cold_start()

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

        After the broker subscription is established, wires ``_on_broker_reconnect``
        onto the broker's market-data stream backends via ``set_reconnect_hook``.
        Every socket reconnect demotes to ``RESYNCHRONIZING`` and runs history
        recovery — a bare reopen never leaves the feed READY.
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
        # Wire notify_reconnect onto broker market backends (B0).
        # Idempotent: repeated calls overwrite with the same callable.
        # Defensive getattr: paper broker and test fakes have no such method.
        set_hook = getattr(self._broker, "set_reconnect_hook", None)
        if callable(set_hook):
            set_hook(self._reconnect_cb)

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
        self._supervisor.disconnected()

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
            # AC-17 / lane 5c, first link in the chain. A tick carries no order
            # identity yet — the strategy mints the CorrelationId from this event
            # when it turns the tick into a signal — so this records the event
            # that the id is derived from, plus whatever id is already in scope
            # (a replay/reconcile thread may be driving this feed under one).
            #
            # DEBUG, not INFO: the feed runs at tick frequency, and an INFO line
            # per tick would bury the order lifecycle it exists to lead into. An
            # operator tracing one order raises the level and filters on the id.
            log.debug(
                "trace feed.tick",
                extra={
                    "stage": "feed.tick",
                    "instrument": quote.instrument.instrument_id,
                    "ltp": str(quote.ltp.value),
                    "event_timestamp": (
                        quote.timestamp.isoformat() if quote.timestamp else None
                    ),
                    "correlation_id": get_correlation_id(),
                },
            )
            self._bus.publish(quote)
            received_at = datetime.now(UTC)
            with self._last_tick_lock:
                self._last_tick[quote.instrument.instrument_id] = received_at
            self._supervisor.record_event(received_at)
            recorder = self._recorder
            if recorder is not None:
                recorder.record(quote)
            tracker = self._integrity
            if tracker is not None:
                self._publish_violations(quote.instrument, tracker.on_quote(quote))

    def _on_depth(self, depth: Depth) -> None:
        if depth.instrument.instrument_id in self._instruments:
            self._bus.publish(depth)
            tracker = self._integrity
            if tracker is not None:
                self._publish_violations(depth.instrument, tracker.on_depth(depth))

    def on_bar_frame(self, frame: Any) -> None:
        """Account a closed bar frame for integrity tracking.

        Called by the bar aggregator when a frame closes.  Detects
        ``MISSING_BAR`` violations and triggers the fail-closed recovery
        coordinator when a gap is confirmed.
        """
        tracker = self._integrity
        if tracker is None:
            return
        violations = tracker.on_bar(frame)
        if violations:
            self._publish_violations(None, violations)

    def _publish_violations(
        self,
        instrument: Instrument | None,
        violations: tuple[Any, ...],
    ) -> None:
        """Surface integrity violations on the bus as observation-only events.

        After publishing each event, checks whether any violation warrants an
        automatic recovery: ``LARGE_JUMP`` (quote-level gap) and ``MISSING_BAR``
        (bar-level gap) both indicate tape discontinuity and trigger the
        fail-closed recovery coordinator in a daemon thread.
        """
        trigger_recovery = False
        for kind in violations:
            kind_str = str(getattr(kind, "value", kind))
            self._bus.publish(
                FeedGapDetected(
                    instrument=instrument,
                    kind=kind_str,
                )
            )
            if kind_str in RECOVERY_TRIGGERS:
                trigger_recovery = True
        if trigger_recovery:
            self._trigger_recovery()

    def _trigger_recovery(self) -> None:
        """Trigger the recovery coordinator in a daemon thread (non-blocking).

        Guards against double-triggering with an in-flight flag — not by reading
        ``RESYNCHRONIZING``, because reconnect already demotes to that state
        before recovery is kicked.
        """
        recovery = self._claim_recovery()
        if recovery is None:
            return

        def _run() -> None:
            try:
                recovery.recover()
            finally:
                self._release_recovery()

        threading.Thread(
            target=_run,
            daemon=True,
            name="feed-auto-recovery",
        ).start()

    # -- readiness -----------------------------------------------------------

    def _prove_cold_start(self) -> Any | None:
        """Drive the fail-closed coordinator for a cold start and gate on it.

        The socket is already up by the time this runs (recovery needs the
        instrument snapshot to fetch anything), so readiness is decided purely
        on the reconciliation. Returns the outcome, or ``None`` when no
        coordinator is wired — nothing can prove continuity then, so the feed
        stays ``RESYNCHRONIZING`` and the order gate stays shut.

        Raises ``FeedNotReadyError`` when the attempt did not succeed, so a
        cold boot refuses to start rather than reporting itself ready.
        """
        if self._recovery is None:
            log.warning(
                "market feed cold start without a recovery coordinator: "
                "continuity cannot be proven, feed stays %s",
                FeedState.RESYNCHRONIZING.value,
            )
            return None
        coordinator = self._claim_recovery()
        if coordinator is None:
            raise FeedNotReadyError(
                "cold start refused: a feed recovery is already in flight"
            )
        try:
            outcome = coordinator.recover()
        except Exception as exc:  # noqa: BLE001 — a refusal, not a crash
            log.exception("feed recovery raised on cold start")
            outcome = None
            reason = str(exc) or type(exc).__name__
            # A coordinator that raised recorded no decision of its own, so the
            # refusal is recorded here — HALTED, not parked in RESYNCHRONIZING.
            self._supervisor.recovery_failed(reason)
        else:
            reason = str(getattr(outcome, "error", "") or "")
        finally:
            self._release_recovery()
        if outcome is not None and bool(getattr(outcome, "ok", False)):
            return outcome
        reason = reason or _UNPROVEN_CONTINUITY
        # A coordinator that reported failure but left the supervisor READY must
        # not be able to admit orders: the decision this path makes is the one
        # that stands.
        if self._supervisor.ready:
            self._supervisor.recovery_failed(reason)
        raise FeedNotReadyError(f"cold start refused: {reason}")

    def _claim_recovery(self) -> Any | None:
        """Claim the single recovery slot; ``None`` when unwired or busy.

        One reconciliation at a time. The in-flight flag is what stops a socket
        reopen or an integrity violation from starting a second one while a
        decision for the same generation is still being made — and it is read
        as a lock rather than inferred from ``RESYNCHRONIZING``, because
        reconnect demotes to that state before recovery is kicked.
        """
        # Bind the checked, non-None recovery to a local: callers use it on
        # another thread (``_trigger_recovery``), where mypy would not carry
        # the ``is None`` guard on the optional attribute into the closure.
        recovery = self._recovery
        if recovery is None:
            return None
        with self._recovery_lock:
            if self._recovery_in_flight:
                return None
            self._recovery_in_flight = True
            if self._supervisor.state is not FeedState.RESYNCHRONIZING:
                self._supervisor.recovery_started()
        return recovery

    def _release_recovery(self) -> None:
        """Release the recovery slot claimed by :meth:`_claim_recovery`."""
        with self._recovery_lock:
            self._recovery_in_flight = False

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
                self._supervisor.mark_stale()
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


__all__ = [
    "FeedNotReadyError",
    "FeedRegistry",
    "MarketFeed",
    "StaleFeed",
    "normalize_depth",
]
