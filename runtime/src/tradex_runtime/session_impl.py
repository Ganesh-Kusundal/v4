"""TradingSession — main entry point for the v4 trading platform.

Lifecycle: NEW -> READY -> STOPPED.
Consumers access engine, broker, bus, cache directly (service layer removed).

Ported from v3 SDK session (WS-B, FDS 05 §5, D-8/D-9/D-15/D-16/D-17).

Lives here, not behind a facade, so that ``tradex_runtime`` no longer depends
on ``tradex_trading``. ``tradex_runtime.session`` re-exports it and remains the
name callers should import.
"""

from __future__ import annotations

import logging
from collections.abc import Callable, Sequence
from dataclasses import replace
from datetime import date, datetime
from decimal import Decimal
from enum import StrEnum
from typing import TYPE_CHECKING, Any

from tradex_config.schema import AppConfig
from tradex_domain import BrokerId, SessionStateError
from tradex_domain.capabilities import BrokerCapabilities
from tradex_domain.errors import CapabilityNotSupportedError, OrderRejectedError
from tradex_domain.instruments import (
    Commodity,
    Currency,
    Equity,
    Future,
    Index,
    Instrument,
    Option,
)
from tradex_domain.protocols import BrokerAdapter
from tradex_domain.strategy import ScannerDefinition
from tradex_domain.value_objects import Price
from tradex_execution.engine import ExecutionEngine
from tradex_execution.trading_cache import TradingCache
from tradex_reactive.bus import ReactiveBus
from tradex_reactive.thread_safe_bus import ThreadSafeReactiveBus

if TYPE_CHECKING:
    # Type-only: used solely in the ``_subscriptions`` annotation below, which
    # is a string under ``from __future__ import annotations``. Same package, so
    # this is a self-edge at worst and never a cross-package layering concern.
    from tradex_runtime.streaming import StreamSubscription

log = logging.getLogger(__name__)


class SessionState(StrEnum):
    """Session lifecycle states."""

    NEW = "NEW"
    READY = "READY"
    STOPPED = "STOPPED"


# ---------------------------------------------------------------------------
# TradingSession
# ---------------------------------------------------------------------------


class TradingSession:
    """Main entry point for the v4 trading platform.

    Lifecycle: NEW -> READY -> STOPPED.
    Services are only available in READY state.
    """

    def __init__(
        self,
        broker: BrokerAdapter,
        bus: ReactiveBus | ThreadSafeReactiveBus,
        engine: ExecutionEngine,
        cache: TradingCache,
        broker_id: BrokerId,
        mode: str = "paper",
        scanner_engine: object | None = None,
        scanner_definitions: Sequence[ScannerDefinition] | None = None,
        strategy_engine: object | None = None,
        strategy_registry: object | None = None,
        stream_backend: object | None = None,
        live_orders_enabled: bool = True,
        fill_bridge: object | None = None,
        market_feed: object | None = None,
        feed_supervisor: object | None = None,
        master_scheduler: object | None = None,
        mark_to_market: object | None = None,
        metrics: object | None = None,
        writer_lock: object | None = None,
        feed_monitor: object | None = None,
        feed_recovery: object | None = None,
        reconciliation_monitor: object | None = None,
        event_store: object | None = None,
        validation_report_store: object | None = None,
    ) -> None:
        self._broker = broker
        self._bus = bus
        self._engine = engine
        self._cache = cache
        self._broker_id = broker_id
        self._mode = mode
        self._metrics = metrics
        self._state = SessionState.NEW
        self._subscriptions: list[StreamSubscription] = []
        self._scanner_engine = scanner_engine
        self._scanner_definitions = tuple(scanner_definitions or ())
        self._strategy_engine = strategy_engine
        #: Wired in by the composition path (``live()``/``boot``) via the
        #: constructor — no post-init private mutation [REF-5].
        self._strategy_registry = strategy_registry
        self._stream_backend = stream_backend
        self._live_orders_enabled = live_orders_enabled
        #: AC-10 / lane 2f: the live-order gate closure, built once from
        #: ``live_orders_enabled``. It used to be built by ``_make_order_gate``
        #: and never called, so the documented "one config flip stops all order
        #: flow" control did not exist. Built here and exposed as
        #: ``order_gate`` so a caller can assert the control fires without any
        #: foreign private write.
        self._order_gate = self._make_order_gate()
        #: LiveFillBridge translating broker order-stream updates into bus
        #: OrderFilled events (live fills reaching the OMS — HIGH-4).
        self._fill_bridge = fill_bridge
        #: Wired in by the composition path (``live()``/``boot``) via the
        #: constructor — no post-init private mutation [REF-5].
        self._market_feed = market_feed
        self._feed_supervisor = feed_supervisor
        #: Automatic stale-poll daemon. Owned here so no caller has to remember
        #: to poll ``MarketFeed.check_stale()``; started in ``start()`` and
        #: stopped in ``stop()`` (never by a client release).
        self._feed_monitor = feed_monitor
        #: Fail-closed reconnect recovery coordinator (history replay → READY).
        #: Driven from three places, all sharing ``MarketFeed``'s in-flight
        #: guard: a socket reconnect (broker hook), an integrity violation
        #: (``FeedGapDetected``), and the owned stale-feed monitor's poll — which
        #: hands a quote-side DEGRADED feed to it on the poll after detection, so
        #: a stale tape is not a terminal DEGRADED. ``session.recover_feed()``
        #: remains the explicit manual entry point.
        self._feed_recovery = feed_recovery
        #: Daily instrument-master refresh daemon (live brokers only). Passed
        #: in by ``TradingSession.live()`` when the broker carries a cached
        #: master loader; stopped here so a long-running session re-downloads
        #: the master (new option series post monthly expiry) without leaking.
        self._master_scheduler = master_scheduler
        #: Quote-driven position marking service. The composition root owns
        #: construction; the session owns lifecycle teardown.
        self._mark_to_market = mark_to_market
        #: Single-writer lock for live mode. Released in stop() so every
        #: teardown path (context manager, explicit stop(), RuntimeContext)
        #: clears the lockfile without monkey-patching.
        self._writer_lock = writer_lock
        #: AC-17 / lane 2e: the durable event log, also injected into
        #: ``ExecutionEngine``. The engine owns the *writes*; the safety
        #: surface needs the *reads* (``replay("orders")``) to time the
        #: UNKNOWN-order ages and to find the last ``ReconciliationResult``.
        #: Without this the route answered both from an empty list and
        #: reported every age as ``None``.
        self._event_store = event_store
        #: AC-17 / lane 2e: the ``tradex_validation_report`` table, read to
        #: answer per-strategy promotion standing. Injected rather than built
        #: here: the store's ``rehydrate`` seam needs ``TradeValidationReport``
        #: from ``tradex_analytics``, and ``tradex_runtime`` must not depend on
        #: ``tradex_analytics`` (see tests/test_import_boundaries.py). The
        #: composition that can see both builds it and passes it in.
        self._validation_report_store = validation_report_store
        #: Automatic reconciliation daemon. Owned here for the same reason as
        #: the feed monitor: no caller has to remember to poll it, and the
        #: session tears it down in ``stop()`` before the broker it reads is
        #: torn down, so a poll cannot observe a half-stopped broker.
        #:
        #: Injected, never constructed here. The seams that drive it
        #: (``build_reconciliation_monitor_seams``) live in ``tradex_interfaces``
        #: because they bind the adoption policy to the OMS book, and
        #: ``tradex_runtime`` may not import ``tradex_interfaces`` (see
        #: ``tests/test_import_boundaries.py``). The composition root that can
        #: see both builds one and passes it here; ``None`` leaves reconciliation
        #: to the on-demand route, which re-derives a read on each request.
        self._reconciliation_monitor = reconciliation_monitor
        #: AC-17 / lane 2e: the drift items from the most recent reconcile.
        #: ``engine.reconcile`` returns them and startup used to log them and
        #: drop them, so the safety surface always fell through to re-deriving
        #: from scratch. Retained so a *checked* reconciliation reports what it
        #: actually found. ``None`` means "never reconciled", which the route
        #: distinguishes from "reconciled and found nothing".
        self._last_reconciliation: tuple[object, ...] | None = None
        #: Explicit stop hooks registered by the composition root. ``before``
        #: runs ahead of feed/broker teardown (strategy registry stop_all);
        #: ``after`` runs once the feed is down (bar aggregator dispose, tape
        #: close). Replaces per-instance ``session.stop`` monkey-patching.
        self._before_stop_hooks: list[Callable[[], None]] = []
        self._after_stop_hooks: list[Callable[[], None]] = []

    def add_stop_hook(
        self,
        hook: Callable[[], None],
        *,
        phase: str = "after",
    ) -> None:
        """Register a teardown callback owned by the composition root.

        ``phase="before"`` runs before feed/broker teardown (e.g. strategy
        registry ``stop_all``). ``phase="after"`` runs after the feed is
        stopped so no tick can race a dispose/close. Hooks are best-effort:
        a failure is logged and the remaining hooks still run.
        """
        if phase not in ("before", "after"):
            raise ValueError(f"stop hook phase must be 'before' or 'after', got {phase!r}")
        target = self._before_stop_hooks if phase == "before" else self._after_stop_hooks
        target.append(hook)

    def start(self) -> None:
        """Transition to READY only after every owned monitor has started."""
        if self._state == SessionState.READY:
            return
        if self._state not in (SessionState.NEW,):
            raise SessionStateError(
                f"Cannot start session in {self._state} state (must be NEW)"
            )
        sid = getattr(self, "_session_id", id(self))
        log.info("Session %s starting", sid)

        started: list[object] = []
        try:
            for monitor in (self._feed_monitor, self._reconciliation_monitor):
                if monitor is None:
                    continue
                monitor.start()  # type: ignore[attr-defined]
                started.append(monitor)
        except Exception:
            for monitor in reversed(started):
                try:
                    monitor.stop()  # type: ignore[attr-defined]
                except Exception:  # pragma: no cover – defensive unwind
                    log.warning("monitor unwind failed", exc_info=True)
            raise

        self._state = SessionState.READY

    def stop(self) -> None:
        """Transition to STOPPED state. Dispose all subscriptions.

        Idempotent: calling stop() on an already-stopped session is a no-op.
        Composition-root ``add_stop_hook`` callbacks run around the body:
        ``before`` ahead of feed teardown, ``after`` once the feed is down
        (and still when the session never left NEW, so tape/aggregator
        dispose cannot be skipped by a failed boot).
        """
        if self._state == SessionState.STOPPED:
            return  # already stopped
        self._run_stop_hooks(self._before_stop_hooks, "before")
        try:
            # Release the single-writer lock (live mode only) regardless of
            # whether the session reached READY. Boot acquires the lock before
            # session.start(); if boot fails partway (e.g. critical drift) the
            # session stays NEW but the lock must still be released.
            if self._writer_lock is not None:
                try:
                    self._writer_lock.release()  # type: ignore[attr-defined]
                except Exception:  # pragma: no cover – defensive teardown
                    log.warning("writer lock release failed", exc_info=True)
                self._writer_lock = None
            if self._state == SessionState.NEW:
                self._shutdown_engine()
                self._state = SessionState.STOPPED
                return  # never started, nothing else to stop
            for sub in self._subscriptions:
                sub.cancel()
            self._subscriptions.clear()
            # Stop the stale-feed poll before the feed itself is torn down so a
            # poll cannot observe a half-stopped feed.
            if self._feed_monitor is not None:
                try:
                    self._feed_monitor.stop()  # type: ignore[attr-defined]
                except Exception:  # pragma: no cover – defensive teardown
                    log.warning("feed stale monitor stop failed", exc_info=True)
                self._feed_monitor = None
            # Stop the reconciliation poller before the broker is torn down so a
            # poll cannot observe a half-stopped broker. Same ordering as the feed
            # monitor above for the same reason.
            if self._reconciliation_monitor is not None:
                try:
                    self._reconciliation_monitor.stop()  # type: ignore[attr-defined]
                except Exception:  # pragma: no cover – defensive teardown
                    log.warning("reconciliation monitor stop failed", exc_info=True)
                self._reconciliation_monitor = None
            if self._market_feed is not None:
                try:
                    self._market_feed.stop()  # type: ignore[attr-defined]
                except Exception:  # pragma: no cover – defensive teardown
                    log.warning("market feed stop failed", exc_info=True)
            if self._mark_to_market is not None:
                try:
                    self._mark_to_market.close()  # type: ignore[attr-defined]
                except Exception:  # pragma: no cover – defensive teardown
                    log.warning("mark-to-market stop failed", exc_info=True)
            self._shutdown_engine()
            self._bus.dispose()
            if self._fill_bridge is not None:
                try:
                    self._fill_bridge.close()  # type: ignore[attr-defined]
                except Exception:  # pragma: no cover – defensive
                    pass
            if self._stream_backend is not None:
                try:
                    # Note: for live brokers this is the same object as the
                    # broker's cached order backend, which ``broker.close()``
                    # below closes again — backends' close() is idempotent.
                    self._stream_backend.close()  # type: ignore[attr-defined]
                except Exception:  # pragma: no cover – defensive
                    pass
            # Tear down the daily master-refresh daemon (live brokers only).
            if self._master_scheduler is not None:
                try:
                    self._master_scheduler.stop()  # type: ignore[attr-defined]
                except Exception:  # pragma: no cover – defensive teardown
                    log.warning("master refresh scheduler stop failed", exc_info=True)
                self._master_scheduler = None
            # Tear down the broker's WebSocket sockets (market/depth/order feeds).
            # Nothing else ever closes them, so without this the daemon receive
            # loops and the reconnect machinery stay alive — minting fresh tokens
            # forever — after the session is stopped.
            # REF-14: a protocol call, not a duck-type probe — BrokerAdapter
            # declares close(), so the attribute always exists on a conforming
            # broker; the try/except keeps teardown defensive.
            try:
                self._broker.close()
            except Exception:  # pragma: no cover – defensive teardown
                log.warning("broker close failed", exc_info=True)
            self._state = SessionState.STOPPED
            sid = getattr(self, '_session_id', id(self))
            log.info("Session %s stopped", sid)
        finally:
            self._run_stop_hooks(self._after_stop_hooks, "after")
            self._before_stop_hooks.clear()
            self._after_stop_hooks.clear()

    def _run_stop_hooks(self, hooks: list[Callable[[], None]], phase: str) -> None:
        for hook in hooks:
            try:
                hook()
            except Exception:  # pragma: no cover – defensive teardown
                log.warning("session %s-stop hook failed", phase, exc_info=True)

    def _shutdown_engine(self) -> None:
        """Halt order intake and release the engine's durable handles."""
        shutdown = getattr(self._engine, "shutdown", None)
        if callable(shutdown):
            try:
                shutdown()
            except Exception:  # pragma: no cover – defensive teardown
                log.warning("execution engine shutdown failed", exc_info=True)

    def _check_ready(self) -> None:
        if self._state != SessionState.READY:
            raise SessionStateError(f"Session is {self._state}, must be READY")

    # --- bind_execution_engine (v3 parity) ------------------------------------

    def bind_execution_engine(self, execution_engine: ExecutionEngine) -> None:
        """Attach the canonical execution spine to the running session."""
        if self._state not in {SessionState.READY, SessionState.STOPPED}:
            raise SessionStateError("execution engine binding requires a started session")
        self._engine = execution_engine

    # --- Properties ---

    @property
    def order_gate(self) -> Callable[[], None]:
        """The live-order gate: raises while order flow is closed.

        Returns a zero-arg callable that raises ``OrderRejectedError`` when
        ``live_orders_enabled`` is false and returns normally when it is true.
        """
        return self._order_gate

    @property
    def state(self) -> SessionState:
        """Current session state."""
        return self._state

    @property
    def broker_id(self) -> BrokerId:
        """Broker identifier."""
        return self._broker_id

    def strategy_registry(self) -> object | None:
        """Registered strategy runtimes, or ``None`` when not wired.

        Public accessor so lifecycle inspection does not reach into
        ``session._strategy_registry`` [REF-5].
        """
        return self._strategy_registry

    @property
    def event_store(self) -> object | None:
        """The durable OMS event log, or ``None`` when persistence is off.

        The same store ``ExecutionEngine`` appends every lifecycle event to.
        Exposed for read-only inspection (AC-17's safety surface replays the
        ``orders`` stream to date UNKNOWN orders and to find the last
        reconciliation); the engine keeps sole ownership of the writes.
        """
        return self._event_store

    @property
    def validation_report_store(self) -> object | None:
        """The validation-report store, or ``None`` when not wired.

        Injected by the composition root that can reach
        ``tradex_analytics`` (see ``__init__``); ``tradex_runtime`` may not
        import it, so a session cannot build one itself.
        """
        return self._validation_report_store

    @property
    def last_reconciliation(self) -> tuple[object, ...] | None:
        """Drift items from the last reconcile this session ran, if any.

        ``None`` means *never reconciled* — which is not the same as
        "reconciled and found nothing", and the safety surface keeps the two
        apart via ``reconciliation_checked``.
        """
        return self._last_reconciliation

    def record_reconciliation(self, drifts: Sequence[object]) -> None:
        """Retain *drifts* from a completed reconcile run.

        Called by the composition root after ``engine.reconcile(...)`` returns.
        The engine returns the items and startup used to log them and drop
        them, so nothing downstream could tell "we checked and found this
        drift" from "we have never checked". Copying to a tuple also freezes
        the result, so a caller mutating the list it passed cannot change what
        the safety surface later reports.
        """
        self._last_reconciliation = tuple(drifts or ())

    @property
    def capabilities(self) -> BrokerCapabilities:
        """Broker capabilities."""
        return self._broker.capabilities

    @property
    def bus(self) -> ReactiveBus | ThreadSafeReactiveBus:
        """Reactive message bus (thread-safe facade for live sessions)."""
        return self._bus

    @property
    def metrics(self) -> object | None:
        """Boot-time MetricsRegistry (for Prometheus exposition), if wired."""
        return self._metrics

    @property
    def cache(self) -> TradingCache:
        """The OMS book the reconciliation monitor reads and adopts into.

        Exposed so a composition root outside ``tradex_runtime`` can build the
        adoption seams, which need the book and the broker together
        (``build_reconciliation_monitor_seams``). Neither is constructable from
        this package alone without importing ``tradex_interfaces``, which the
        package boundary forbids.
        """
        return self._cache

    @property
    def engine(self) -> ExecutionEngine:
        """Execution engine."""
        return self._engine

    @property
    def broker(self) -> BrokerAdapter:
        """Broker adapter."""
        return self._broker

    @property
    def strategy_engine(self) -> object | None:
        """Strategy engine (auto-registered discovered strategies)."""
        return self._strategy_engine

    @property
    def mode(self) -> str:
        """Execution mode."""
        return self._mode

    # --- internal helpers -----------------------------------------------------

    def _make_order_gate(self) -> Callable[[], None]:
        """Return a closure that enforces the live-order gate (D-17)."""
        live_orders = self._live_orders_enabled

        def gate() -> None:
            if not live_orders:
                raise OrderRejectedError("live order gate disabled")

        return gate

    def assert_order_gate_open(self) -> None:
        """Raise if order flow is closed. The boot-time proof that the control
        is wired: an open gate must not raise, a closed one must."""
        self._order_gate()

    # --- instrument factories (D-1: pure value constructors, any state) -------

    def equity(self, exchange: str, symbol: str) -> Equity:
        return Equity.of(exchange, symbol)

    def index(self, exchange: str, symbol: str) -> Index:
        return Index.of(exchange, symbol)

    def future(self, exchange: str, underlying: str, expiry: date) -> Future:
        return Future.of(exchange, underlying, expiry)

    def option(
        self,
        exchange: str,
        underlying: str,
        expiry: date,
        strike: Price | Decimal | float,
        right: str,
    ) -> Option:
        strike_value: Decimal | float = strike.value if isinstance(strike, Price) else strike
        return Option.of(exchange, underlying, expiry, strike_value, right)

    def currency(self, exchange: str, symbol: str) -> Currency:
        return Currency.of(exchange, symbol)

    def commodity(self, exchange: str, symbol: str) -> Commodity:
        return Commodity.of(exchange, symbol)

    # --- Factory classmethods ---

    @classmethod
    def paper(
        cls,
        broker_id: str = "PAPER",
        *,
        bus: ReactiveBus | None = None,
        config: AppConfig | None = None,
        reconciliation_monitor: object | None = None,
        paper_clock: Callable[[], datetime] | None = None,
    ) -> TradingSession:
        """Create a paper trading session with a simulated broker.

        Parameters
        ----------
        broker_id : str
            Broker identifier (default: "paper").
        bus : ReactiveBus | None
            Optional reactive bus instance.
        config : AppConfig | None
            Optional config honoring ``execution`` fees/slippage (HIGH-6b
            parity with ``runtime.startup.boot``). Defaults to zero-cost.
        paper_clock : Callable[[], datetime] | None
            Pins the simulated session time. ``PaperBroker``'s session gate
            refuses a fill outside 09:15-15:30 IST, so a run that does not pin
            this inherits the host clock and stops filling after the close.

        Returns
        -------
        TradingSession
            A session configured for paper trading.
        """
        # Thin wrapper over the single composition root [REF-6]: the broker
        # is injected so paper() keeps its always-simulated semantics even
        # for a non-PAPER label; everything else is boot()'s wiring.
        from tradex_brokers.paper.adapter import PaperBroker

        # Function-local to keep the ``paper()`` factory import-light. This is a
        # same-package import (``tradex_runtime`` -> ``tradex_runtime``), so it
        # is excluded from the package import graph as a self-edge and cannot
        # close a cycle. It used to go through the ``tradex_trading.runtime``
        # shim purely to dodge the old runtime -> trading -> runtime ring; that
        # indirection retires with the move of TradingSession into
        # tradex_runtime.
        from tradex_runtime.startup import boot

        cfg = config or AppConfig()
        cfg = replace(
            cfg,
            mode="paper",
            broker_id=BrokerId(broker_id) if broker_id else BrokerId.PAPER,
        )
        # Historical minimal component set: no reactive strategy/scanner wiring.
        # ``paper_clock`` reaches both the session's own broker and the
        # book-matching fill source, so a run that pins a simulated session time
        # pins every gate that reads the clock.
        return boot(
            cfg, bus=bus, broker=PaperBroker(clock=paper_clock),
            wire_strategies=False,
            reconciliation_monitor=reconciliation_monitor,
            paper_clock=paper_clock,
        )

    @property
    def feed_supervisor(self) -> object | None:
        """Feed health state for live mode, or ``None`` for offline modes."""
        return self._feed_supervisor

    @property
    def feed_monitor(self) -> object | None:
        """Owned stale-feed poll daemon (``None`` for offline modes/stopped)."""
        return self._feed_monitor

    @property
    def feed_recovery(self) -> object | None:
        """Reconnect recovery coordinator (``None`` when history is unwired)."""
        return self._feed_recovery

    def recover_feed(self) -> Any:
        """Run the fail-closed feed recovery sequence after a reconnect.

        Returns the coordinator's outcome (``ok``, ``bars_replayed``,
        ``missing_bars``, ``error``). Raises ``CapabilityNotSupportedError``
        when the session has no recovery coordinator (offline modes, or a live
        feed without a history source — in which case a reconnect can never be
        proven continuous and the feed must stay degraded).
        """
        if self._feed_recovery is None:
            raise CapabilityNotSupportedError(
                "no feed recovery coordinator bound to this session"
            )
        return self._feed_recovery.recover()  # type: ignore[attr-defined]

    @property
    def market_feed(self) -> Any | None:
        """Live broker tick bridge (quotes + depth) bound to this session's bus.

        ``None`` for paper sessions. Start streaming with
        ``session.market_feed.start(instruments)`` or the convenience
        ``session.start_market_feed(instruments)``.
        """
        return self._market_feed

    def start_market_feed(self, instruments: Sequence[Instrument]) -> None:
        """Start the live quote + depth feed for *instruments*.

        Raises
        ------
        CapabilityNotSupportedError
            If this session has no live market feed (paper mode).
        """
        if self._market_feed is None:
            raise CapabilityNotSupportedError(
                "no live market feed bound to this session (paper mode)"
            )
        self._market_feed.start(instruments)  # type: ignore[attr-defined]

    @classmethod
    def live(
        cls,
        broker_id: BrokerId,
        *,
        bus: ReactiveBus | None = None,
        confirm: bool = False,
    ) -> TradingSession:
        """Create a live trading session.

        Parameters
        ----------
        broker_id : BrokerId
            Broker identifier.
        bus : ReactiveBus | None
            Optional reactive bus instance.
        confirm : bool
            Must be True to confirm live trading (safety gate).

        Returns
        -------
        TradingSession
            A session configured for live trading.

        Raises
        ------
        ValueError
            If confirm is not True.
        """
        if not confirm:
            raise ValueError(
                "Live trading requires explicit confirmation. "
                "Pass confirm=True to proceed."
            )

        # Thin wrapper over the single composition root [REF-6]: boot() owns
        # broker construction (env auth), the thread-safe bus, fill source,
        # stream backend, fill bridge, market feed and master scheduler.
        # See the note in paper() above: function-local and same-package, so it
        # is not a cross-package edge.
        from tradex_runtime.startup import boot

        cfg = AppConfig(
            broker_id=broker_id,
            mode="live",
            live_enabled=True,
        )
        return boot(cfg, bus=bus, wire_strategies=False)


    # --- Context manager protocol ---

    def __enter__(self) -> TradingSession:
        """Context manager entry."""
        return self

    def __exit__(self, *args: object) -> None:
        """Context manager exit — stop the session."""
        try:
            self.stop()  # type: ignore[attr-defined]
        except Exception:
            pass

__all__ = [
    "SessionState",
    "TradingSession",
]
