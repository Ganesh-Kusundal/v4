"""Runtime boot — composition root for the v4 trading platform.

This is the ONLY place that wires all components together.
Fail-closed: any error during boot prevents session creation.

``boot`` also wires the extensions auto-discovery contract: every strategy
discovered in ``strategy/extensions`` is registered into a
``ReactiveStrategyEngine``, and every discovered scanner definition is bound
into the session's scanner engine (via ``scanner_engine``).
"""

from __future__ import annotations

import atexit
import logging
from collections.abc import Sequence
from dataclasses import dataclass
from decimal import Decimal
from pathlib import Path
from typing import TYPE_CHECKING, Any, cast

from tradex_brokers import DhanBroker, PaperBroker, UpstoxBroker
from tradex_domain import BrokerId, OrderRejectedError
from tradex_domain.instruments import Instrument
from tradex_domain.protocols import BrokerAdapter

if TYPE_CHECKING:
    from tradex_runtime.session_impl import TradingSession
    from tradex_runtime.writer_lock import SingleWriterLock

from tradex_config.permissions import assert_secret_file_mode
from tradex_config.schema import FEED_INSTRUMENTS_ENV, AppConfig
from tradex_execution.engine import (
    ExecutionEngine,
    MemoryIdempotencyGuard,
    RiskManager,
)
from tradex_execution.fees import FeeCalculator
from tradex_execution.fill_sources import (
    BrokerFillSource,
    PaperFillSource,
    SimulatedFillSource,
)
from tradex_execution.slippage import PercentageSlippageModel
from tradex_reactive.bus import ReactiveBus
from tradex_reactive.thread_safe_bus import ThreadSafeReactiveBus
from tradex_strategy.core.engine import ReactiveStrategyEngine
from tradex_strategy.core.scanner import DEFAULT_SCANNER_CALL_INTERVAL, ScannerEngine
from tradex_strategy.extensions import all_scanners, all_strategies

from tradex_runtime.logging_config import setup_structured_logging
from tradex_runtime.metrics import MetricsRegistry

log = logging.getLogger(__name__)


def _broker_available_cash(broker: Any) -> Decimal | None:
    """Available cash from broker funds, or None when it cannot be read.

    ``None`` means *unknown* — a funds call that fails, an adapter that does
    not report a balance, or a malformed account. The fail-closed cash gate
    treats unknown as a stop rather than as a zero or an infinite balance.
    """
    try:
        account = broker.get_account()
    except Exception:
        log.warning("Broker funds unavailable; cash gate will deny BUYs")
        return None
    balance = getattr(account, "balance", None)
    if balance is None:
        return None
    amount = getattr(balance, "amount", None)
    if amount is None:
        return None
    try:
        return Decimal(str(amount))
    except (ArithmeticError, TypeError, ValueError):
        # A balance that is not a number is an unreadable balance, and an
        # unreadable balance is unknown — never zero and never "sufficient".
        log.warning("Broker funds unreadable; cash gate will deny BUYs")
        return None


def _broker_matches_config(config: AppConfig, broker: Any) -> bool:
    """Validate that the injected broker matches the configured broker identity.

    Built-in adapters are checked by class. A custom injected adapter may expose
    ``provider`` or ``broker_id`` for an explicit identity check; otherwise its
    injection is treated as an intentional factory override and still must
    satisfy the runtime adapter contract at session start.
    """
    name = config.broker.name.strip().lower()
    if name == "paper":
        return isinstance(broker, PaperBroker)
    if isinstance(broker, DhanBroker):
        return name == "dhan"
    if isinstance(broker, UpstoxBroker):
        return name == "upstox"
    # Custom adapter — check provider/broker_id attribute.
    identity = getattr(broker, "provider", getattr(broker, "broker_id", None))
    if identity is None:
        return False
    value = getattr(identity, "value", identity)
    return str(value).strip().lower() == name


@dataclass(frozen=True, slots=True)
class RuntimeContext:
    """Bundled runtime components returned by ``boot_context``.

    Holds the config, session, engine, strategy engine, bus, and broker so
    callers can manage the full lifecycle (including ``close()``).

    M7: ``writer_lock`` is carried on the TradingSession itself (passed
    through the constructor by the composition root). ``close()`` releases
    it via ``session.stop()`` — no module global, no monkey-patch.
    """

    config: AppConfig
    session: TradingSession
    engine: ExecutionEngine
    strategy_engine: ReactiveStrategyEngine | None
    bus: ReactiveBus | ThreadSafeReactiveBus
    broker: BrokerAdapter
    writer_lock: SingleWriterLock | None = None

    def close(self) -> None:
        """Stop the session and release runtime resources.

        PE-10: uses ShutdownCoordinator for ordered teardown — strategies
        first (stop producing events), then engine (drain pipeline), then
        session (release resources), then writer lock (last).

        Each phase runs in its own try/except so one component's failure
        does not prevent the remaining phases from executing.
        """
        from tradex_runtime.shutdown import ShutdownCoordinator

        coord = ShutdownCoordinator()

        # Phase 1: strategies — stop producing new events
        if self.strategy_engine is not None:
            coord.register(
                "strategies", priority=1,
                action=self.strategy_engine.dispose_all,  # type: ignore[attr-defined]
            )

        # Phase 2: engine — drain the reactive pipeline
        if hasattr(self, "engine") and self.engine is not None:
            coord.register("engine", priority=2, action=self.engine.shutdown)

        # Phase 3: session — release resources (includes writer lock for live)
        coord.register("session", priority=3, action=self.session.stop)

        coord.shutdown()


def boot(
    config: AppConfig | None = None,
    *,
    bus: ReactiveBus | ThreadSafeReactiveBus | None = None,
    broker: Any | None = None,
    wire_strategies: bool = True,
    feed_instruments: Sequence[Instrument] | None = None,
    validation_report_store: Any = None,
    reconciliation_monitor: Any = None,
    reconciliation_monitor_factory: Any = None,
) -> TradingSession:
    """Fail-closed boot: compose all components into a ready TradingSession.

    This is the composition root — the ONLY place that wires everything together.
    Extension strategies are registered into a ``ReactiveStrategyEngine`` and
    discovered scanner definitions are bound into the session's scanner engine.

    Parameters
    ----------
    config : AppConfig | None
        Application configuration. If None, uses defaults (paper mode).
    feed_instruments : Sequence[Instrument] | None
        The tape a live session must be able to prove before it starts (AC-20).
        The composition root cold starts ``MarketFeed`` for this set, so the feed
        reaches ``READY`` because a history reconciliation ran, and the boot is
        refused when it does not. ``None`` (the default) falls back to
        ``config.feed_instruments``, which is where ``$TRADEX_FEED_INSTRUMENTS``
        lands — an operator names the tape, the platform does not guess one. An
        empty tape in live mode refuses the boot rather than serving a session
        that rejects every order. Offline modes ignore it entirely: they
        construct no feed.
    validation_report_store :
        A :class:`~tradex_execution.validation_report_store.ValidationReportStore`
        (or any object with the same read surface) to expose as
        ``session.validation_report_store`` for the AC-17 safety route.

        **Injected, never constructed here.** The store's ``rehydrate`` seam has
        to produce a validation report, and the report class lives in
        ``tradex_analytics`` — a package ``tradex_runtime`` may not import (see
        ``tests/test_import_boundaries.py``). A composition root that can see
        both builds one with
        :func:`tradex_interfaces.safety_report_store.build_validation_report_store`
        and passes it here. ``None`` leaves the accessor ``None``, which the
        safety surface reports as *absent* rather than as "not promotable".
    reconciliation_monitor :
        A :class:`~tradex_runtime.reconciliation_monitor.ReconciliationMonitor`
        (or any object with the same ``start()``/``stop()`` surface) that
        continuously reconciles the OMS book against the venue and *adopts*
        venue positions when the read is clean.

        **Injected, never constructed here.** The seams that drive it —
        ``build_reconciliation_monitor_seams`` — live in ``tradex_interfaces``
        because they bind the adoption policy to the OMS book and to
        ``derive_venue_adoption``, and ``tradex_runtime`` may not import
        ``tradex_interfaces`` (see ``tests/test_import_boundaries.py``). A
        composition root that can see both builds one and passes it here, the
        same shape as ``validation_report_store``.

        ``None`` leaves reconciliation to the on-demand safety route, which
        re-derives a venue read on each request. The daemon is not load-bearing
        for the *correctness* of that route; it is load-bearing for its
        *timeliness*, because without it drift is only ever observed when
        somebody asks.
    reconciliation_monitor_factory :
        A callable ``(broker, cache, engine, metrics) -> monitor`` invoked inside
        the boot, *before* the session starts, so the returned monitor is
        running by the time ``boot()`` returns.

        This exists because the two halves of the daemon live on opposite sides
        of the package boundary: ``build_reconciliation_monitor_seams`` is in
        ``tradex_interfaces`` and ``ReconciliationMonitor`` is in
        ``tradex_runtime``, which may not import ``tradex_interfaces``. A
        composition root that can see both passes a closure here; ``boot()``
        supplies the components from the session it is already building, and the
        session then owns the monitor's lifecycle.

        Overrides ``reconciliation_monitor`` when both are given, because a
        factory is strictly more capable: it sees the real broker and cache
        rather than objects a caller had to construct separately.

        ``None`` (the default) leaves the monitor unset unless
        ``reconciliation_monitor`` is passed.

    Returns
    -------
    TradingSession
        A fully initialized session in READY state.

    Raises
    ------
    Exception
        Any error during boot prevents session creation (fail-closed).
    """
    cfg = config or AppConfig()

    # AC-17 / lane 5c: turn on the structured logging that already existed.
    # ``setup_structured_logging`` was defined and unit-tested but had NO
    # production call site, so every real run emitted plain stdlib lines and the
    # correlation id was a contextvar nobody ever set. It is installed here,
    # before the first log line below, so boot's own output is structured too.
    #
    # First in the function on purpose: an operator who asks for JSON logs
    # should not have to read the first dozen lines of the boot sequence in the
    # other format, and a root logger configured once cannot be un-configured
    # by anything that follows.
    setup_structured_logging(
        "tradex",
        json_format=cfg.logging.json_format,
        level=cfg.logging.level_value(),
    )
    log.info("Booting runtime context...")

    # 0. Safety gates
    valid_modes = {"paper", "backtest", "replay", "live"}
    if cfg.mode not in valid_modes:
        raise ValueError(
            f"unknown mode: {cfg.mode!r}, must be one of {sorted(valid_modes)}"
        )
    # AC-10 / lane 2f: in paper as well as live, no risk limit is optional. A
    # limit left at None refuses the boot; an operator who genuinely wants one
    # unlimited says so with the explicit UNBOUNDED sentinel. Backtest and
    # replay are offline (no venue to stop) and are not gated here.
    #
    # Checked FIRST, ahead of the other live preconditions, so a missing risk
    # limit is named as the missing risk limit rather than surfacing as some
    # later, more incidental gate while the operator is still filling in the
    # rest of a fresh config.
    if cfg.mode in ("paper", "live"):
        cfg.risk.require_limits()
    if cfg.mode == "live" and cfg.broker_id == BrokerId.PAPER:
        raise ValueError("live mode requires a non-paper broker")
    if cfg.mode == "live" and not cfg.live_enabled:
        raise ValueError("live mode requires live_enabled=true in config")
    # AC-17 / lane 5d: a credential file readable beyond its owner is a
    # defect, and the token layer only guarantees the mode of files IT created.
    # An operator-created ``.env.local`` was never stat-ed, so a world-readable
    # secret was entirely unguarded. Detect and refuse; never silently chmod,
    # because quietly fixing it hides the misconfiguration the operator needs
    # to know about. A MISSING file is legitimate (paper runs with no
    # credentials), so absence never blocks a boot.
    for _secret in cfg.secret_files():
        assert_secret_file_mode(_secret)
    if cfg.mode == "live" and not cfg.persistence.path:
        raise ValueError("live mode requires persistence path")

    # 1. Create broker. Live brokers bind a real transport via the standard
    # interface (build_broker_from_env); paper/backtest/replay construct the
    # transport-less adapter directly (paper is transport-less by design).
    # An injected broker (seam for tests and the SDK factories) wins.
    if broker is None:
        if cfg.mode == "live":
            from tradex_runtime.live import build_broker_from_env
            broker = build_broker_from_env(cfg.broker_id.value)
        else:
            broker = {
                BrokerId.PAPER: PaperBroker,
                BrokerId.DHAN: DhanBroker,
                BrokerId.UPSTOX: UpstoxBroker,
            }[cfg.broker_id]()

    # 2. Create metrics registry
    metrics = MetricsRegistry()

    # 3. Create reactive bus. Live mode serializes publishes (RLock): the
    # broker feed thread, engine worker threads, and API callers all publish
    # concurrently, and a plain RxPY Subject must never be driven from two
    # threads at once. The feed thread's own quote → pipeline chain is
    # reentrant on the same thread, so the lock never delays it.
    if bus is None:
        bus = ReactiveBus(metrics=metrics)
        if cfg.mode == "live":
            # bus is still the plain ReactiveBus just created above.
            bus = ThreadSafeReactiveBus(cast(ReactiveBus, bus))

    # 3b. Execution costs — the SAME slippage + fee models used by
    # BacktestEngine when callers configure them (HIGH-6b parity: backtest and
    # reactive paper/live net P&L must agree). Off by default (zero-cost model).
    slippage_model: Any = None
    if cfg.execution.slippage_bps is not None:
        slippage_model = PercentageSlippageModel(
            pct=cfg.execution.slippage_bps / Decimal(10000),
        )
    fee_calculator: FeeCalculator | None = (
        FeeCalculator() if cfg.execution.fees_enabled else None
    )

    # 4. Create fill source based on mode. In paper the two Flow-B fill paths
    #    are selectable per run (AC-14 / spec lane 2d): "book_matching" walks the
    #    paper broker's simulated book (the oracle), "slippage_model" fills at
    #    the live quote through the slippage model. Both satisfy the same
    #    FillSource seam, so everything downstream (risk, ledger, fees) is
    #    identical and the only difference is how the price was found.
    fill_source: Any
    if cfg.mode == "paper":
        from tradex_runtime.paper_book_fill_source import (
            BOOK_MATCHING,
            PaperBookFillSource,
        )

        if cfg.execution.fill_path == BOOK_MATCHING:
            fill_source = PaperBookFillSource(
                spread_bps=cfg.execution.book_spread_bps,
            )
        else:
            fill_source = PaperFillSource(slippage_model=slippage_model)
    elif cfg.mode == "backtest":
        fill_source = SimulatedFillSource(slippage_model=slippage_model)
    elif cfg.mode == "live":
        fill_source = BrokerFillSource(broker)
    elif cfg.mode == "replay":
        fill_source = SimulatedFillSource(slippage_model=slippage_model)
    else:
        raise ValueError(f"unknown mode: {cfg.mode}")

    # 4b. Durability (R1/R2) — opt-in via cfg.persistence.path:
    #   - SQLiteIdempotencyGuard: correlation IDs survive restarts.
    #   - SQLiteOrderStore: every order lifecycle event mirrors the OMS state
    #     into SQLite; on boot the store is restored into the cache BEFORE the
    #     session starts, then reconciled against the broker book post-start.
    #   - SQLiteEventStore (B2): append-only event log for full OMS event
    #     sourcing; replayed at boot to rebuild the OMS projection before
    #     broker reconciliation. All three share the same DB file.
    guard: Any = MemoryIdempotencyGuard()
    order_store: Any = None
    event_store: Any = None
    if cfg.persistence.path:
        from tradex_execution.sqlite_event_store import SQLiteEventStore
        from tradex_execution.sqlite_store import (
            SQLiteIdempotencyGuard,
            SQLiteOrderStore,
        )
        guard = SQLiteIdempotencyGuard(cfg.persistence.path)
        order_store = SQLiteOrderStore(cfg.persistence.path)
        event_store = SQLiteEventStore(cfg.persistence.path)

    # 4c. Single-writer guard (R2) — live only. Rate limiters are per-process;
    # two live writers on one account can jointly breach provider limits.
    writer_lock: Any = None
    if cfg.mode == "live":
        from tradex_runtime.writer_lock import SingleWriterLock

        writer_lock = SingleWriterLock(
            Path("runtime/live") / f"{cfg.broker_id.value.lower()}.writer.lock"
        )
        writer_lock.acquire()  # fail-closed if another live process is running
        atexit.register(writer_lock.release)  # stale-PID auto-clear covers crashes
        try:
            return _boot_tail(
                cfg, bus, broker, wire_strategies, writer_lock,
                fill_source=fill_source, metrics=metrics, guard=guard,
                fee_calculator=fee_calculator, order_store=order_store,
                event_store=event_store, feed_instruments=feed_instruments,
                validation_report_store=validation_report_store,
                reconciliation_monitor=reconciliation_monitor,
                reconciliation_monitor_factory=reconciliation_monitor_factory,
            )
        except BaseException:
            # C3: best-effort rollback so a failed live boot never
            # leaves a connected transport, a stranded writer lockfile,
            # or a subscribed bus.
            _safe_teardown(session=None, broker=broker, bus=bus, writer_lock=writer_lock)
            raise

    # C3: non-live branch also rolls back. The session is built inside
    # _boot_tail, so we pass session=None — the helper tolerates that
    # and only closes the broker and disposes the bus. (There is
    # no writer lock to release in non-live mode.)
    try:
        return _boot_tail(
            cfg, bus, broker, wire_strategies, None,
            fill_source=fill_source, metrics=metrics, guard=guard,
            fee_calculator=fee_calculator, order_store=order_store,
            event_store=event_store, feed_instruments=feed_instruments,
            validation_report_store=validation_report_store,
            reconciliation_monitor=reconciliation_monitor,
            reconciliation_monitor_factory=reconciliation_monitor_factory,
        )
    except BaseException:
        _safe_teardown(session=None, broker=broker, bus=bus, writer_lock=None)
        raise


def _safe_teardown(
    session: Any | None,
    broker: Any,
    bus: Any,
    writer_lock: Any | None,
) -> None:
    """Best-effort rollback used by every boot failure path (C3).

    Closes the broker, disposes the bus, stops the session (if
    built), and releases the writer lock (if any). Every call is
    wrapped in its own try/except so one failure does not mask another.
    Used by both the live and non-live branches of :func:`boot`.
    """
    # Brokers define close() (the same surface session.stop() uses);
    # there is no disconnect() on any adapter.
    close = getattr(broker, "close", None)
    if callable(close):
        try:
            close()
        except Exception:
            log.warning("broker close during boot rollback failed", exc_info=True)
    bus_dispose = getattr(bus, "dispose", None)
    if callable(bus_dispose):
        try:
            bus_dispose()
        except Exception:
            log.warning("bus dispose during boot rollback failed", exc_info=True)
    if session is not None:
        stop = getattr(session, "stop", None)
        if callable(stop):
            try:
                stop()
            except Exception:
                log.warning("session stop during boot rollback failed", exc_info=True)
    if writer_lock is not None:
        try:
            writer_lock.release()
        except Exception:
            log.warning("writer lock release during boot rollback failed", exc_info=True)


def _boot_tail(
    cfg: AppConfig,
    bus: ReactiveBus | ThreadSafeReactiveBus,
    broker: Any,
    wire_strategies: bool,
    writer_lock: Any,
    *,
    fill_source: Any,
    metrics: MetricsRegistry,
    guard: Any,
    fee_calculator: FeeCalculator | None,
    order_store: Any,
    event_store: Any = None,
    feed_instruments: Sequence[Instrument] | None = None,
    validation_report_store: Any = None,
    reconciliation_monitor: Any = None,
    reconciliation_monitor_factory: Any = None,
) -> TradingSession:
    """Steps 5→end of :func:`boot`, extracted so the live path can be
    rollback-wrapped. The body is the original ``boot()`` logic unchanged.
    """
    # Function-local: keeps this module importable on its own and defers the
    # session module until boot actually runs (it is same-package, so this is
    # not a cross-package layering edge).
    from tradex_runtime.feed_supervisor import FeedSupervisor
    from tradex_runtime.session_impl import TradingSession

    feed_supervisor = FeedSupervisor() if cfg.mode == "live" else None

    # 5. Create risk manager
    # AC-9: the per-strategy budgets are resolved by the strategy identifier,
    # not the ``name@version`` text, and are persisted so a restart does not
    # reset a tripped daily-loss/drawdown breaker. The baseline store shares
    # the same SQLite file as the order store (persistence.path) — one DB, one
    # subsystem. In-memory (no persistence path) it stays None and the in-RAM
    # baseline is used, exactly as before.
    baseline_store = None
    if cfg.persistence.path:
        from tradex_execution.risk_baseline_store import SQLiteRiskBaselineStore

        baseline_store = SQLiteRiskBaselineStore(cfg.persistence.path)
    # AC-10: ``UNBOUNDED`` becomes the RiskManager's own "no limit"
    # sentinel here, so the documented opt-out actually means unbounded instead
    # of -1, which would deny everything. See ``RiskConfig.effective_limits``.
    limits = cfg.risk.effective_limits()
    risk_manager = RiskManager(
        max_order_value=limits["max_order_value"],
        max_position_value=limits["max_position_value"],
        max_orders_per_minute=limits["max_orders_per_minute"],
        reject_unknown_market_value=cfg.risk.reject_unknown_market_value,
        max_daily_loss_amt=limits["max_daily_loss_amt"],
        max_drawdown_pct=limits["max_drawdown_pct"],
        # AC-10 / lane 2f: the master gate. When False, ``check()`` refuses
        # EVERY submit before any other gate runs. Bound to the same
        # ``AppConfig.live_orders_enabled`` the session gate reads, so the
        # documented "one config flip stops all order flow" is one flag.
        #
        # This gate is about order flow that leaves the machine, so it is bound
        # in live only. Paper trading IS order flow against a simulated venue
        # and is the whole point of the mode; closing its gate because the
        # operator never set a LIVE flag would make paper unrunnable by default
        # and would be a different control wearing this one name. The live
        # default is False, so a live session comes up refusing every submit
        # until the operator deliberately opens it.
        live_orders_enabled=(cfg.live_orders_enabled or cfg.mode != "live"),
        # Live risk is never allowed to silently operate on an unmarked book.
        require_fresh_marks=(cfg.mode == "live" or cfg.risk.require_fresh_marks),
        max_mark_age_seconds=cfg.risk.max_mark_age_seconds,
        # AC-9: per-strategy risk budgets, resolved by strategy id.
        budgets=cfg.risk.budgets,
        # C3: same registry as /metrics — rejection reasons + exposure gauges.
        metrics=metrics,
        # AC-9: durable per-strategy capital/drawdown baselines.
        baseline_store=baseline_store,
    )

    # 6. Create execution engine
    live_fail_closed = cfg.mode == "live"
    engine = ExecutionEngine(
        bus=bus, fill_source=fill_source, risk_manager=risk_manager, metrics=metrics,
        idempotency_guard=guard, fee_calculator=fee_calculator,
        feed_supervisor=feed_supervisor,
        event_store=event_store,
        require_risk=live_fail_closed,
        require_feed=live_fail_closed,
        require_durable_events=live_fail_closed,
    )
    # Bind the OMS cache as the position source so ``max_position_value`` is
    # enforced against live cumulative exposure (qty * avg_price + incoming).
    risk_manager.set_positions_provider(engine.cache.all_positions)
    risk_manager.set_price_provider(engine.cache.get_quote)
    # Quote-driven MTM is attached before the session is exposed. This keeps
    # the cache, risk gate, API, and stream projections on one position book.
    from tradex_execution.mark_to_market import MarkToMarketService

    mark_to_market = MarkToMarketService(engine.cache, bus, metrics=metrics)
    # Keep the service on the session so shutdown can detach its subscription.
    # Paper MARKET fills reference the same cache quotes that drive MTM, so
    # paper fills and marks share one market reference.
    if cfg.mode == "paper":
        fill_source.bind_cache(engine.cache)
    # Cash gate: live and paper must know the real balance. A BUY whose
    # notional cannot be covered is denied, and an unavailable balance denies
    # too (fail closed) rather than skipping the gate. Backtest/replay own
    # their own CashLedger and bypass this gate.
    opening_cash: Decimal | None = None
    if cfg.mode in ("paper", "live"):
        provider = cfg.risk.cash_provider or (
            lambda: _broker_available_cash(broker)
        )
        risk_manager.bind_cash_provider(provider)
        risk_manager.fail_closed_cash = True
        # Seed the engine's ledger from the same source, but only as a cold-boot
        # opening balance. This runs AFTER event recovery (see below) so a
        # restart does not anchor again on top of the balance it just folded:
        # the log is authoritative, and re-anchoring would grow it and make the
        # next fold double-count the fills.
        opening_cash = provider()
    engine.kill_switch = cfg.kill_switch_default

    # H7: pipeline errors must not be silent. The engine publishes
    # ``ErrorOccurred`` on the bus (engine.py:517, 524, 533); without a
    # subscriber those events are dropped. Wire a logger + counter so
    # operators see pipeline failures and the runtime exposes a metric.
    if metrics is not None:
        errors_total = metrics.counter("engine.errors.total")
        from tradex_domain.events import ErrorOccurred
        def _on_error(event):
            log.error("pipeline error: %s", event.error)
            errors_total.inc()
        # of_type() filters the bus stream to ErrorOccurred only;
        # the bus.subscribe() raw form would receive every event.
        bus.of_type(ErrorOccurred).subscribe(on_next=_on_error)

    # 6a. Order durability (R1) — restore persisted orders into the OMS cache
    # BEFORE the session starts, then mirror every lifecycle event into the
    # store. Subscriptions die with bus.dispose() on session.stop().
    if order_store is not None:
        from tradex_execution.sqlite_store import attach_order_persistence

        order_store.load_into(engine.cache)
        # metrics= so a store that stops being written shows up as a counter
        # rather than only in the log: attach_order_persistence deliberately
        # swallows persistence failures so they cannot break trading, which
        # otherwise leaves a dead store indistinguishable from a healthy one.
        attach_order_persistence(bus, engine.cache, order_store, metrics=metrics)

    # 6b. Event-sourced OMS recovery (B2/C1) — replay the durable event log to
    # rebuild the OMS projection. Runs after the snapshot restore (6a) so
    # event-derived state takes precedence; runs BEFORE broker reconciliation
    # (step 9) so the diff is computed against the freshest local book.
    # New lifecycle events are appended by ExecutionEngine._append (store is
    # injected above) — do not also bus-subscribe or classic events double-append.
    if event_store is not None:
        _recover_oms_from_events(event_store, engine.cache, engine)

    # Cold boot only. The log is authoritative: when recovery already restored
    # a balance, that value wins over the broker figure (which may be sampled at
    # a different instant), and re-anchoring would duplicate the opening
    # balance. A log that already holds FILLS is never anchored from broker
    # funds either — that balance is net of those fills, so replaying them
    # again would double-count the history. Cash stays unknown instead, and the
    # fail-closed gate keeps denying until an operator records an anchor.
    if opening_cash is not None and engine.cash_anchor_missing:
        if engine.cache.all_orders():
            log.error(
                "Event log holds fills but no opening balance; refusing to "
                "anchor from broker funds. Cash remains unknown and BUYs are "
                "denied until an operator records a CashAccountInitialized."
            )
        else:
            engine.restore_cash(opening_cash, re_anchor=True)

    # 6d. Strategy engine — register every auto-discovered extension strategy
    # so user strategies (strategy/extensions) run without touching core.
    # Discovery already isinstance-filters against the Strategy protocol, so
    # registration cannot realistically fail; any error still aborts boot
    # (fail-closed — nothing is swallowed).
    #
    # C4: each raw strategy is also wrapped in a StrategyRuntime and
    # registered into a StrategyRegistry so the full CREATED→RUNNING→STOPPED
    # lifecycle is tracked.  The registry is started after session.start()
    # (start_all) and stopped inside session.stop() (stop_all, wired via a
    # closure below).  No broker or SQLite inside strategies — the runtime
    # enforces that by owning all I/O surfaces.
    strategy_engine: Any = None
    strategy_registry: Any = None
    if wire_strategies:
        from tradex_strategy.artifacts import StrategyArtifact
        from tradex_strategy.registry import StrategyRegistry
        from tradex_strategy.runtime import StrategyRuntime

        strategy_engine = ReactiveStrategyEngine(
            bus, engine.submit,
            fill_reference=cfg.execution.fill_reference,
        )
        strategy_registry = StrategyRegistry()
        # The discovered examples are bound to a hardcoded instrument and place
        # real orders. They stay discoverable for the chart/backtest route, but
        # a session only trades them when explicitly asked — otherwise a live
        # account would run `sma_cross_example` on RELIANCE out of the box, and
        # SELL (never cash-gated) would put on a real short.
        strategies = all_strategies if cfg.execution.auto_register_examples else ()
        for strategy in strategies:
            artifact = StrategyArtifact(
                strategy_id=strategy.strategy_id,
                version=str(getattr(strategy, "version", "1.0.0")),
            )
            sid = strategy.strategy_id

            def _stop_one(sid: str = sid) -> None:
                # AC-8: stopping this strategy halts its order flow NOW,
                # disposes its subscriptions, and cancels its WORKING orders
                # (UNKNOWN-aware). Open positions and their protective legs
                # are left alone — flattening is a separate explicit command.
                # The engine does the per-strategy filtering itself, so the
                # whole order book can be handed over unchanged.
                strategy_engine.stop_strategy(
                    sid,
                    cancel=engine.cancel,
                    working_orders=engine.cache.all_orders(),
                )

            strategy_registry.register(
                StrategyRuntime(
                    artifact,
                    strategy,
                    on_pause=lambda sid=sid: strategy_engine.pause_strategy(sid),
                    on_resume=lambda sid=sid: strategy_engine.resume_strategy(sid),
                    on_stop=lambda sid=sid: _stop_one(sid),
                ),
            )
            strategy_engine.register(strategy)

    # 7. Connect broker (loads instruments/registry for live brokers)
    broker.connect()
    # Live must prove wire auth before the session is READY. connect() only
    # loads instruments; a locally-unexpired JWT can still 401 at the venue.
    if cfg.mode == "live":
        verify = getattr(broker, "verify_connection", None)
        if not callable(verify) or not verify():
            raise RuntimeError(
                "live broker failed wire authentication probe (verify_connection)"
            )

    # 7b. Bind the order/portfolio stream backend (live brokers only) so
    # callers using session.bus for OrderPlaced/OrderCancelled/OrderModified
    # (and live WS backends) reach the broker WebSocket instead of falling
    # back to a stub. Paper/backtest brokers expose no backend; any wiring
    # failure degrades to the existing bus fallback.
    stream_backend = None
    fill_bridge: Any = None
    # AC-12: order-stream frame-age watchdog. Stamped by the fill bridge on
    # every order frame; polled by the same owned monitor thread that already
    # watches the quote tape. Built here (before the bridge) so the bridge can
    # stamp it, and reused in the market-feed block below to join the poll.
    order_watchdog: Any = None
    if cfg.mode == "live":
        try:
            sb = getattr(broker, "stream_backend", None)
            if callable(sb):
                stream_backend = sb()
        except Exception as exc:
            raise RuntimeError("live order-stream backend unavailable") from exc
        from tradex_runtime.feed_monitor import OrderStreamWatchdog
        order_watchdog = OrderStreamWatchdog(feed_supervisor)
        # Live fill bridge: translate broker order-stream updates into bus
        # OrderFilled events so live fills reach the OMS (HIGH-4). Live boot
        # fails closed if this safety-critical bridge cannot be installed.
        if stream_backend is not None and hasattr(
            stream_backend, "subscribe_orders"
        ):
            try:
                from tradex_runtime.live_fill_bridge import (
                    LiveFillBridge,
                    TradeBookFillIdResolver,
                )

                # Every live provider must expose a trade-book identity seam.
                # Dhan calls it ``trade_book``; Upstox calls it
                # ``get_trade_book``. The resolver normalizes their row keys
                # and strict mode fails closed if identity is unavailable.
                # When the broker does not expose one, fall back to a non-strict
                # resolver so live fills can still land through the bridge (they
                # will be stamped with no fill_id and rely on the engine's
                # composite fingerprint, which is the documented backward-
                # compatible behavior until all live adapters ship a seam).
                trade_book = getattr(broker, "trade_book", None)
                if not callable(trade_book):
                    trade_book = getattr(broker, "get_trade_book", None)
                resolver = None
                if callable(trade_book):
                    resolver = TradeBookFillIdResolver(trade_book, strict=False)
                else:
                    log.warning(
                        "live broker does not expose an executed-trade identity seam; "
                        "live fills will use the engine's composite dedup fingerprint"
                    )
                fill_bridge = LiveFillBridge(
                    bus, engine, stream_backend.subscribe_orders,
                    trade_id_resolver=resolver,
                    unsubscribe=getattr(stream_backend, "unsubscribe", None),
                    # Same registry as /metrics. Without it the bridge counts
                    # its average-price fallbacks (AC-7) on an object nothing
                    # ever reads, so the share of live fills booked without
                    # per-trade detail is invisible to an operator.
                    metrics=metrics,
                    # Stamp the AC-12 order-stream liveness clock on every
                    # frame so a silently dead order socket is detectable.
                    on_frame=order_watchdog.record_frame,
                )
            except Exception as exc:
                raise RuntimeError("live fill bridge unavailable at boot") from exc
        else:
            # Live mode with no order-stream backend: broker fills can never
            # reach the OMS. Fail closed rather than run a diverging book.
            raise ValueError("live mode requires order-stream backend")

    # 7c. Scanner engine — bind the market provider so the session can run
    # every auto-discovered extension scanner. Backtest/replay modes scan
    # the local parquet datalake (offline, full Nifty universe) instead of
    # the broker; paper/live keep live data.
    scanner_engine: Any = None
    if wire_strategies:
        if cfg.mode in ("backtest", "replay"):
            from tradex_market_data.market_provider import ParquetMarketProvider
            # AC-15: the scanner's parquet store gets the session registry, so a
            # corrupt partition quarantined while the scanner reads the local
            # lake is counted on the very registry ``GET /metrics`` renders.
            scanner_market: Any = ParquetMarketProvider(metrics=metrics)
            # Local disk: a scan here costs no venue quota, so pacing it
            # would only add wall clock to a backtest.
            scanner_call_interval = 0.0
        else:
            scanner_market = broker
            # AC-13: paper/live scan through the *broker*, i.e. the same
            # venue quota the live feed is drawing from.  A 500-symbol scan
            # is 500 back-to-back calls into it, which is the degradation
            # the scanner's own rate limit exists to stop.  The interval
            # is stated here — the composition root is the only place that
            # knows the provider is a venue — rather than inferred from the
            # provider's class inside the engine.
            scanner_call_interval = DEFAULT_SCANNER_CALL_INTERVAL
        scanner_engine = ScannerEngine(
            market=scanner_market,
            min_call_interval=scanner_call_interval,
        )
        # G7: stream Candle events into the scanner's rolling buffer so
        # _history() can serve incremental data instead of re-fetching.
        from tradex_domain.market import Candle as _Candle
        bus.of_type(_Candle).subscribe(on_next=scanner_engine.consume)

    # 7d. Backtest loader — backtest/replay modes expose an offline datalake
    # backtest tool on the session: ``session.backtest.load()``/``.run()``
    # assemble ``BacktestEngine`` inputs from the parquet store (multi-symbol,
    # no broker). Paper/live keep None (live data paths instead).
    backtest_loader: Any = None
    if cfg.mode in ("backtest", "replay"):
        from tradex_market_data.backtest_loader import ParquetBacktestLoader
        # AC-15: same session registry as the scanner, so a quarantine hit
        # while a backtest loads the lake is counted on the served metrics.
        backtest_loader = ParquetBacktestLoader(metrics=metrics)

    # 8. Create session — strategies registered, scanner engine bound
    # so ``session._scanner_engine.run(definition)`` works.
    # Live mode additionally wires the market feed and the daily master
    # refresh scheduler HERE (moved from ``TradingSession.live`` — the
    # composition root owns all wiring [REF-6]). The scheduler is started
    # only after the session is READY.
    market_feed: Any = None
    master_scheduler: Any = None
    feed_monitor: Any = None
    feed_recovery: Any = None
    tape_recorder: Any = None
    if cfg.mode == "live":
        from tradex_runtime.feed_integrity import FeedIntegrityTracker
        from tradex_runtime.feed_monitor import FeedStaleMonitor, SymbolCadenceMonitor
        from tradex_runtime.feed_recovery import (
            FeedRecoveryCoordinator,
            ParquetHistoryProvider,
        )
        from tradex_runtime.market_feed import MarketFeed
        from tradex_runtime.master_lifecycle import (
            InstrumentRefreshScheduler,
            MasterLoader,
        )

        # Reuse the supervisor already wired into ExecutionEngine — a second
        # instance would leave require_feed gating on a different READY state
        # than MarketFeed / session readiness.
        if feed_supervisor is None:
            raise RuntimeError(
                "live boot missing FeedSupervisor before MarketFeed wiring",
            )
        integrity = FeedIntegrityTracker(metrics=metrics)
        # Tick tape: a JSONL record of every quote the feed accepts, for the
        # backtest/live parity suite and the two-paper-fill-path comparison to
        # consume. ``open_tape_recorder`` returns None when TRADEX_RECORD_TAPE
        # is not "1", which is the "no recorder exists" signal: the feed then
        # has nothing to record to. Named ``tape_recorder``, not ``recorder``:
        # "tape" already means the feed-instruments set here
        # (TRADEX_FEED_INSTRUMENTS), and this is a different tape entirely.
        from tradex_runtime.tape_recorder import open_tape_recorder

        tape_recorder = open_tape_recorder(broker=str(cfg.broker_id))
        market_feed = MarketFeed(
            broker=broker,
            bus=bus,
            supervisor=feed_supervisor,
            integrity=integrity,
            recorder=tape_recorder,
        )
        # AC-12 / lane 3d: per-symbol cadence. The feed-level 30 s threshold
        # cannot see a symbol that stopped ticking *entirely* — it is absent
        # from ``MarketFeed._last_tick`` rather than stale in it — and neither
        # wire format carries a sequence number, so a dropped frame is
        # indistinguishable from an illiquid minute. A symbol needs one tick per
        # 3m session-anchored window; one that the instrument master reports as
        # halted or delisted is exempt, and every exemption is counted in
        # feed_liveness_exempt_total so the blind spot stays visible.
        #
        # It joins the SAME owned poll as the tape and the order stream rather
        # than owning a second timer, and it does not demote the feed: one
        # illiquid scrip must not shut the order gate for a healthy tape.
        instrument_registry = getattr(broker, "registry", None)

        def _master_status(instrument: Any) -> object:
            """The instrument master's lifecycle state for *instrument*.

            Read from the broker's instrument registry, which is where a master
            load puts a row's metadata (``BaseBroker.load_instruments``). A
            broker that exposes no registry, or an instrument it never loaded,
            yields ``None`` — "not exempt", so the symbol keeps its cadence
            contract rather than being waved through on a missing lookup.
            """
            iid = getattr(instrument, "instrument_id", None)
            meta_of = getattr(instrument_registry, "meta", None)
            if iid is None or not callable(meta_of):
                return None
            return meta_of(iid).get("master_status")

        cadence_monitor = SymbolCadenceMonitor(
            instruments=lambda: list(market_feed.wanted_instruments()),
            ticks=lambda instrument: market_feed.last_tick(instrument),
            status_of=_master_status,
            metrics=metrics,
        )
        # The session owns the monitor's lifecycle; nothing else has to
        # remember to poll check_stale(). The same thread also checks the
        # order-stream watchdog (AC-12) and per-symbol cadence (lane 3d) — one
        # poll, three questions, one lifecycle.
        feed_monitor = FeedStaleMonitor(
            market_feed,
            metrics=metrics,
            order_watchdog=order_watchdog,
            cadence_monitor=cadence_monitor,
        )
        # Recovery is fail-closed: without a history provider it can only ever
        # fail, which is exactly the guarantee we want from a bare reconnect.
        try:
            history_provider: Any = ParquetHistoryProvider()
        except Exception:
            log.warning("live history provider unavailable", exc_info=True)
            history_provider = None
        feed_recovery = FeedRecoveryCoordinator(
            market_feed,
            supervisor=feed_supervisor,
            history_provider=history_provider,
            bus=bus,
            metrics=metrics,
        )
        # Wire the coordinator back into the feed so LARGE_JUMP / MISSING_BAR
        # violations auto-trigger recovery (B: socket alone never READY).
        market_feed.set_recovery(feed_recovery)
        loader = getattr(broker, "master_loader", None)
        refresh_hook = getattr(broker, "ensure_master_fresh", None)
        if isinstance(loader, MasterLoader) and callable(refresh_hook):
            master_scheduler = InstrumentRefreshScheduler(
                cfg.broker_id.value.lower(), refresh_hook
            )

    session = TradingSession(
        broker=broker,
        bus=bus,
        engine=engine,
        cache=engine.cache,
        broker_id=cfg.broker_id,
        mode=cfg.mode,
        scanner_engine=scanner_engine,
        scanner_definitions=all_scanners if wire_strategies else (),
        strategy_engine=strategy_engine,
        strategy_registry=strategy_registry,
        stream_backend=stream_backend,
        backtest_loader=backtest_loader,
        fill_bridge=fill_bridge,
        market_feed=market_feed,
        feed_supervisor=feed_supervisor,
        master_scheduler=master_scheduler,
        metrics=metrics,
        mark_to_market=mark_to_market,
        writer_lock=writer_lock,
        feed_monitor=feed_monitor,
        feed_recovery=feed_recovery,
        # Lane 5e: the reconciliation daemon. Injected for the same boundary
        # reason as validation_report_store above — the seam builder lives in
        # tradex_interfaces, which this package may not import. The session owns
        # the lifecycle: started in start(), stopped in stop() before the broker
        # is torn down.
        reconciliation_monitor=reconciliation_monitor
        or (
            reconciliation_monitor_factory(broker, engine.cache, engine, metrics)
            if reconciliation_monitor_factory is not None
            else None
        ),
        # AC-17 / lane 2e: the same store the engine appends to, handed to the
        # session so the safety surface can read the log it needs to date
        # UNKNOWN orders and find the last reconciliation. Before this the
        # route's ``getattr(session, "event_store", None)`` was always None
        # and every age it reported was ``None`` — an order of unknown age,
        # which is precisely the state an operator must not be shown as fresh.
        event_store=event_store,
        # AC-17 / lane 2e: injected, not built. See ``boot``'s docstring — the
        # store's rehydrator needs a report class from tradex_analytics, which
        # this package may not import. The composition root builds it and
        # passes it down.
        validation_report_store=validation_report_store,
        # AC-10 / lane 2f: bind the documented "one config flip stops all
        # order flow" control. ``AppConfig.live_orders_enabled`` is read by
        # ``from_dict`` and was read by nothing else, so the gate that
        # ``_make_order_gate()`` builds was dead. With the flag wired to the
        # session and its RiskManager master gate, flipping it refuses every
        # submit. As with the RiskManager gate, this closes order flow that
        # leaves the machine, so it is bound in live only; paper keeps it open.
        live_orders_enabled=(cfg.live_orders_enabled or cfg.mode != "live"),
    )

    # AC-10 / lane 2f: prove at boot that the order gate actually fires, so
    # "one config flip stops all order flow" is a fact and not a runbook claim.
    # The session builds the gate from the flag it was constructed with; boot
    # only exercises it. Flipping the flag false is a legitimate "stop trading
    # but keep the process" state, so the boot is not refused — the gate is
    # simply already closed, which is exactly what the operator asked for.
    gate_open = cfg.live_orders_enabled or cfg.mode != "live"
    if gate_open:
        session.assert_order_gate_open()  # open gate: must not raise
    else:
        try:
            session.assert_order_gate_open()  # closed gate: must raise
        except OrderRejectedError:
            log.warning(
                "live_orders_enabled=false: the order gate is closed; every "
                "submit on this session is refused by design"
            )

    # 9. Live restart reconciliation (R1) — run BEFORE session.start() so
    # a *new* critical drift never leaves the session in READY (C2).
    # The strategy engine is already subscribed at this point, but the
    # session services are gated on _check_ready(), so the strategy
    # cannot place an order until session.start() transitions to READY.
    # A pre-configured ``kill_switch_default=True`` is NOT a "new" trip;
    # the session still goes READY so the user can observe rejection.
    reconcile_tripped = False
    if cfg.mode == "live":
        if event_store is None:
            log.critical(
                "Refusing to start session: live mode requires a durable "
                "event store (persistence.path must yield SQLiteEventStore)."
            )
            if not engine.kill_switch:
                engine.trip_kill_switch(reason="DURABLE_EVENTS_UNAVAILABLE")
            reconcile_tripped = True
        else:
            reconcile_tripped = _run_startup_reconciliation(broker, engine, session)
        if reconcile_tripped:
            log.critical(
                "Refusing to start session: kill switch tripped during "
                "startup reconciliation. Inspect drift before clearing."
            )
        # AC-12 / lane 3b: after any *subsequent* order/portfolio socket
        # reconnect, re-read orders and positions from the venue through the
        # same reconciliation path (never resume blind). Installed on the
        # order-stream backend, which mixes in AutoReconnectMixin and fires
        # set_reconnect_hook after each successful reopen.
        _install_post_reconnect_reread(broker, engine, stream_backend, session)

    # C4: attach the strategy registry to the session so callers (and tests)
    # can inspect lifecycle state via session.strategy_registry().  The
    # registry is a constructor argument — no post-init private mutation
    # [REF-5].  Wire stop_all into session.stop() via a closure so every
    # teardown path (context manager, explicit stop(), RuntimeContext.close())
    # transitions runtimes to STOPPED before bus subscriptions are disposed.
    # C4 (extended): the tick tape is closed on the same funnel, AFTER the
    # session stop has torn the feed down so no broker thread can be mid-record.
    # The recorder writes line-buffered, so every record already reached the OS
    # even if this never ran; closing is what ends the session with a finished
    # segment rather than one left open. ``close()`` is idempotent, and the
    # recorder is None unless the TRADEX_RECORD_TAPE gate was open, so
    # non-live and un-gated boots take the untouched path.
    if strategy_registry is not None or tape_recorder is not None:
        _reg = strategy_registry
        _recorder = tape_recorder
        _orig_stop = session.stop
        def _stop_with_teardown():
            if _reg is not None:
                _reg.stop_all()
            _orig_stop()  # stops the feed, so no tick can race the close below
            if _recorder is not None:
                try:
                    _recorder.close()
                except Exception:  # pragma: no cover - defensive teardown
                    log.warning("tape recorder close failed", exc_info=True)
        # Intentional per-instance method substitution: the registry teardown and
        # the tape close must run before the normal teardown on every stop path.
        # Rewriting an attribute that is a method on the class is a
        # method-assign, which mypy flags because assigning over a bound method
        # is unsafe in general. It is safe here — ``session`` is a freshly
        # booted instance, and the closure captures the original bound ``stop``.
        # Converting this to a constructor parameter is a session-API change (a
        # product decision), so the substitution is kept and the error suppressed
        # at this one line with that reasoning.
        session.stop = _stop_with_teardown  # type: ignore[method-assign]  # ponytail: closure on instance; upgrade to session ctor param if registry grows a public API

    if not reconcile_tripped:
        # 9b. Cold start the market feed (Flow D step 5, AC-20): start the
        # feed, reach READY, or refuse to start. The feed is constructed and
        # wired to the fail-closed recovery coordinator in step 8, but nothing
        # called ``MarketFeed.start()`` — the coordinator only ever ran on a
        # socket reconnect — so the supervisor sat at RESYNCHRONIZING for the
        # life of the session and every live order was refused with
        # ``feed_not_ready``. The cold start is the one moment the feed proves
        # itself, and this is the one place that can demand it: it runs BEFORE
        # ``session.start()`` so the session is never READY over a feed that
        # has not proven itself, and it raises rather than warns, so a failed
        # proof is a boot that did not happen. It is skipped when there is no
        # feed (paper/backtest/replay construct none) and when reconciliation
        # above already refused, because there is nothing left to trade.
        #
        # The tape is the operator's to name and ``AppConfig`` resolves it from
        # ``$TRADEX_FEED_INSTRUMENTS`` on every construction, so the
        # unattended paths (``TradingSession.live()``, ``quick_start_live.py``,
        # the CLI) trade as soon as the variable is set. The keyword is the
        # override, and an explicit empty sequence stays empty rather than
        # falling back to the environment — a caller that means "no tape" gets
        # the refusal below, not a surprise.
        named_tape = (
            cfg.feed_instruments if feed_instruments is None else feed_instruments
        )
        _cold_start_feed(session, market_feed, named_tape)

        # 10. Start session, then the refresh daemon (started last so nothing
        # after it can strand it).
        session.start()
        # C4: transitions all CREATED runtimes → RUNNING after session is READY.
        if strategy_registry is not None:
            strategy_registry.start_all()
    else:
        # Leave session in NEW; caller can stop() to release resources.
        # Mark the master scheduler's expected-state so it is not started.
        master_scheduler = None

    if master_scheduler is not None:
        master_scheduler.start()

    log.info("Runtime context ready")
    return session


def _cold_start_instruments(
    market_feed: Any, requested: Sequence[Instrument] | None
) -> tuple[Instrument, ...]:
    """The instruments a cold start must prove continuity for, de-duplicated.

    The caller's watchlist first, then whatever the feed is already subscribed
    to: ``MarketFeed.start`` stops before it subscribes, so an instrument some
    other component subscribed before the boot would otherwise be dropped from
    the very set the boot is trying to prove.
    """
    ordered: dict[Any, Instrument] = {}
    existing = market_feed.snapshot_instruments()
    for instrument in (*(requested or ()), *existing):
        ordered.setdefault(instrument.instrument_id, instrument)
    return tuple(ordered.values())


def _cold_start_feed(
    session: Any,
    market_feed: Any,
    requested: Sequence[Instrument] | None,
) -> None:
    """Start the live market feed and let its recovery verdict gate the boot.

    Fail-closed (AC-20, Flow D step 5). ``MarketFeed.start`` drives the same
    recovery coordinator a reconnect drives and refuses with
    ``FeedNotReadyError`` when the tape between the last bar this process saw
    and now cannot be proven; a connected socket proves nothing, so a session
    that could not reconcile must not start. A feed that returns *without* a
    verdict — no coordinator wired, so nothing was proven at all — is refused
    here too rather than read as a pass.

    No feed (paper/backtest/replay) is a no-op. An **empty tape in live mode is
    a refusal**: nothing named this process to stream anything, so there is
    nothing to reconcile and nothing to claim, and a session that comes up
    ``READY`` over a feed that was never started rejects every order with
    ``feed_not_ready`` for the life of the process while looking healthy to
    ``/health/ready``. The tape is the operator's to name — never a default
    index, a scanner universe or the account's own exposure — so the refusal
    says which setting to set instead of guessing on their behalf.
    """
    if market_feed is None:
        return
    instruments = _cold_start_instruments(market_feed, requested)
    if not instruments:
        # Nothing named this process to stream anything, so there is nothing to
        # reconcile and nothing to claim. Handing back a session would come up
        # ``READY`` over a feed that was never started and refuse every order
        # with ``feed_not_ready`` for the life of the process while looking
        # healthy to ``/health/ready`` — an operator who reads a log line is an
        # operator who trades on it. The tape is the operator's to name, so the
        # refusal says which setting to set rather than guessing on their
        # behalf: never a default index, a scanner universe, or the account's own
        # exposure (all three were priced and rejected — see lane c1's report).
        raise ValueError(
            "live boot refused: no tape named, so tape continuity is unproven and "
            f"every order would be refused with feed_not_ready. Set "
            f"${FEED_INSTRUMENTS_ENV}=EXCHANGE:SYMBOL[,EXCHANGE:SYMBOL...] to name "
            "the instruments this session trades (e.g. "
            f"${FEED_INSTRUMENTS_ENV}=NSE:RELIANCE), or pass "
            "boot(feed_instruments=[...]). Paper, backtest and replay need no tape "
            "and are unaffected."
        )
    try:
        session.start_market_feed(instruments)
    except BaseException:
        # The feed subscribed before it could prove itself; stop it so a
        # refused boot leaves no live stream behind a session that never
        # started, then let the refusal out (boot's C3 rollback then closes the
        # broker and releases the writer lock).
        try:
            market_feed.stop()
        except Exception:  # pragma: no cover – defensive teardown
            log.warning("market feed stop after a refused cold start failed",
                        exc_info=True)
        log.critical(
            "Refusing to start session: the market feed could not prove tape "
            "continuity for %d instrument(s) — %s",
            len(instruments), market_feed.supervisor.state.value,
        )
        raise
    if not market_feed.supervisor.ready:
        market_feed.stop()
        log.critical(
            "Refusing to start session: the market feed cold start returned "
            "without reaching READY (state=%s)", market_feed.supervisor.state.value,
        )
        raise RuntimeError(
            "live boot refused: the market feed did not reach READY "
            f"(state={market_feed.supervisor.state.value})"
        )
    log.info(
        "Market feed cold start reached READY for %d instrument(s)",
        len(instruments),
    )


def _reconcile_cash(broker: Any, engine: Any) -> list[Any]:
    """Compare the engine's cash against broker funds.

    The local ledger is authoritative for what this process did; the broker is
    authoritative for what the account actually holds. A difference means one
    of them is wrong — a deposit, withdrawal, or charge the log never saw — so
    it is reported as HIGH drift and stops admission until an operator records
    the venue's figure as a ``CashAccountInitialized`` or ``CashReconciled``.

    Returns no drift when funds cannot be read: the cash gate already denies
    BUYs on an unknown balance, and reporting "unknown" as drift would train
    operators to ignore the signal.
    """
    reconciler = getattr(engine, "_reconciler", None)
    if reconciler is None:
        return []
    try:
        venue = broker.get_account()
    except Exception as exc:  # noqa: BLE001 — unknown funds is the gate's job
        log.warning("startup cash reconcile unavailable: %s", exc)
        return []
    venue_balance = getattr(venue, "balance", None)
    if venue_balance is None:
        return []

    snapshot = engine.cash_snapshot()
    if snapshot.cash is None:
        return []
    # A balance we cannot read as a number is unknown, not a difference. The
    # cash gate already denies BUYs on an unknown balance; reporting it as
    # drift would fire the reconciliation gate on every malformed response.
    try:
        local_amount = Decimal(str(snapshot.cash))
        venue_amount = Decimal(str(venue_balance.amount))
    except (ArithmeticError, TypeError, ValueError):
        log.warning("startup cash reconcile unreadable; treating as unknown")
        return []
    if not (isinstance(venue_amount, Decimal) and venue_amount == venue_amount):
        return []

    from tradex_domain.execution import Account
    from tradex_domain.value_objects import AccountId, Money

    local = Account(
        account_id=getattr(venue, "account_id", AccountId("local")),
        balance=Money(amount=local_amount),
    )
    normalised = Account(
        account_id=getattr(venue, "account_id", AccountId("local")),
        balance=Money(amount=venue_amount),
    )
    drifts = reconciler.compare_funds(local, normalised)
    for item in drifts:
        log.warning(
            "startup cash drift: local=%s venue=%s — a deposit, withdrawal, or "
            "charge is not in the local ledger",
            local_amount, venue_amount,
        )
    return drifts


def _install_post_reconnect_reread(
    broker: Any, engine: Any, stream_backend: Any, session: Any = None
) -> None:
    """Re-read the venue book after every order/portfolio stream reconnect.

    AC-12/3b: a dropped order/portfolio socket reconnects and replays its
    subscription, but the socket carries only *incremental* updates — anything
    that changed at the venue while it was down is never re-delivered. Without
    a re-read the session would resume blind and a resting order or position
    could silently diverge.

    This wires the *existing* reconnect hook (:meth:`set_reconnect_hook`) on
    the order-stream backend to the *existing* reconciliation
    (:func:`_run_startup_reconciliation`), which re-reads
    ``get_orderbook()`` + ``get_positions()``, runs ``engine.reconcile``,
    refreshes the OMS cache, and trips the kill switch on HIGH/CRITICAL drift.
    The hook fires only on a real reconnect (never on the initial connect), so
    a session whose socket never dropped performs no extra re-read.

    The re-read also refreshes the session's retained drift (AC-17 / lane 2e),
    which is why the session is captured: after a reconnect the safety surface
    must report the comparison that was *just* made, not the one from boot.

    Best-effort: backends without a reconnect hook (paper, fakes) are skipped
    and reconciliation failure is already contained by the callee.
    """
    if stream_backend is None:
        return
    set_hook = getattr(stream_backend, "set_reconnect_hook", None)
    if not callable(set_hook):
        return

    def _reread() -> None:
        log.info("order/portfolio stream reconnected; re-reading venue book")
        try:
            _run_startup_reconciliation(broker, engine, session)
        except Exception:  # noqa: BLE001 — hook must not abort the receive loop
            log.warning("post-reconnect venue re-read failed", exc_info=True)

    set_hook(_reread)


def _run_startup_reconciliation(broker: Any, engine: Any, session: Any = None) -> bool:
    """Live-mode startup reconciliation. Extracted so tests can call it directly.

    - Pulls the broker orderbook + positions.
    - Calls ``engine.reconcile`` (side-effect free).
    - Updates the OMS cache from the broker book (status refresh only).
    - Logs every drift; trips the kill switch on HIGH/CRITICAL drift so
      a diverged book never reaches the trading surface.
    - Retains the drift items on *session* (AC-17 / lane 2e) when given, so
      the safety surface reports what this run actually found rather than
      re-deriving from scratch and answering "nothing".

    Returns True iff reconciliation tripped the kill switch that wasn't
    already set by config (i.e. a *new* critical-drift trip). The
    caller uses this to decide whether to refuse ``session.start()`` —
    a pre-configured ``kill_switch_default=True`` is intentional and
    the session must still reach READY (so the user can observe that
    orders are rejected); a reconcile-discovered drift is an
    emergency stop and the session must NOT go live.

    Best-effort: every broker call is wrapped in try/except and degrades
    to a warning. The kill switch is the only hard failure.
    """
    # Snapshot the prior state so we can detect a *new* trip below.
    kill_switch_was_set = bool(engine.kill_switch)
    try:
        book = broker.get_orderbook()
    except Exception as exc:  # noqa: BLE001 — reconcile is best-effort
        log.warning("startup order-book reconcile unavailable: %s", exc)
        book = None
    try:
        broker_positions = broker.get_positions()
    except Exception as exc:  # noqa: BLE001 — reconcile is best-effort
        log.warning("startup position reconcile unavailable: %s", exc)
        broker_positions = None
    if book is None and broker_positions is None:
        log.critical(
            "Trading HALTED: broker orderbook and positions both unavailable "
            "at startup reconciliation — cannot prove local book matches venue."
        )
        engine.trip_kill_switch(reason="RECON_UNAVAILABLE")
        # Always refuse READY: unlike kill_switch_default=True (intentional
        # observe-rejects), recon-unavailable means we cannot verify the book.
        return True
    drifts = engine.reconcile(
        broker_orders=book, broker_positions=broker_positions,
    )
    # AC-17 / lane 2e: retain the outcome before the early return below, so a
    # session that HALTED is not left claiming it has never checked. ``None``
    # on the session is reserved for "never reconciled"; an empty tuple is
    # "reconciled, found nothing", and the safety surface keeps them apart.
    if session is not None and hasattr(session, "record_reconciliation"):
        session.record_reconciliation(drifts)

    # Cash is part of the book: a deposit, withdrawal, or charge at the venue
    # changes what the account can afford, and nothing else in the log records
    # it. Drift here means the local balance is a number we cannot trust.
    drifts.extend(_reconcile_cash(broker, engine))
    if book is not None:
        for row in book:
            current = engine.cache.get_order(row.order_id.value)
            if current is not None and current.status != row.status:
                engine.cache.update_order(row)
    if drifts:
        for item in drifts:
            log.warning("startup drift: %s", item)
        critical = [
            d for d in drifts
            if str(getattr(d, "severity", "")).upper()
            in ("HIGH", "CRITICAL")
        ]
        if critical:
            log.critical(
                "Trading HALTED: %d unreconciled HIGH/CRITICAL drift "
                "item(s) between local book and broker at startup "
                "(e.g. %s). Trip kill switch to prevent trading on a "
                "diverged book.",
                len(critical),
                ", ".join(
                    getattr(d, "key", "") or getattr(d, "symbol", "")
                    for d in critical[:5]
                ),
            )
            engine.trip_kill_switch(
                reason="startup_reconciliation_drift"
            )
    # Only return True if reconciliation *newly* tripped the switch.
    return (not kill_switch_was_set) and bool(engine.kill_switch)



def _recover_oms_from_events(event_store: Any, cache: Any, engine: Any = None) -> None:
    """Replay all events from *event_store* into *cache* (the OMS projection).

    B2: runs BEFORE broker reconciliation so the local book is as fresh as
    possible before the diff is computed. Orders and positions are rebuilt
    via :func:`tradex_execution.recovery.recover_trading_cache`.

    When *engine* is given, the recovered cash is restored into its ledger.
    Without this a restarted process folds the balance correctly and then
    reports ``cash = 0``, because the engine's ledger starts empty and is
    never seeded from the log.
    """
    from tradex_execution.recovery import recover_trading_cache

    result = recover_trading_cache(event_store, cache)
    log.info(
        "OMS event-sourced recovery complete: events_replayed=%d orders_recovered=%d",
        result.events_replayed,
        result.orders_recovered,
    )
    if engine is not None and result.cash is not None:
        # A fold is a restore, not a new opening balance: the anchor is already
        # in the log, so re-anchoring would duplicate it and double-count fills
        # on the next restart.
        engine.restore_cash(result.cash.cash, result.cash.total_fees)
        log.info(
            "Cash restored from event log: cash=%s fees=%s",
            result.cash.cash, result.cash.total_fees,
        )
    elif engine is not None:
        log.warning(
            "Event log carried no cash anchor; the engine reports cash as "
            "unknown (0) and the fail-closed cash gate will deny BUYs.",
        )


def _attach_event_store(bus: Any, event_store: Any) -> None:
    """Subscribe *event_store* to all order lifecycle events on *bus*.

    Every OrderPlaced / OrderFilled / OrderCancelled / OrderModified /
    OrderRejected published on the bus is appended to the durable event log
    so future restarts can replay the full history.

    Append failures are logged and never raised — a durable-log failure must
    not break trading, but it MUST be visible in the operator log.
    Subscriptions die with bus.dispose() on session.stop().
    """
    from tradex_domain.events import (
        OrderCancelled,
        OrderFilled,
        OrderModified,
        OrderPlaced,
        OrderRejected,
    )

    def _append(event: Any) -> None:
        try:
            event_store.append(event)
        except Exception:
            log.exception("OMS event-store append failed; event not durable")

    for event_type in (OrderPlaced, OrderFilled, OrderCancelled, OrderModified, OrderRejected):
        bus.of_type(event_type).subscribe(on_next=_append)


def boot_context(
    config: AppConfig | None = None,
    **boot_kwargs: Any,
) -> RuntimeContext:
    """Boot and return a full RuntimeContext for lifecycle management.

    Like ``boot`` but returns a ``RuntimeContext`` that bundles all components
    and provides a ``close()`` method for clean shutdown. Extra keyword
    arguments (e.g. ``bus=`` or ``broker=``) are forwarded to :func:`boot`.

    M7: the writer_lock is carried on the TradingSession (set by the
    composition root in ``_boot_tail``), so ``session.stop()`` releases
    it without a module global or monkey-patch.
    """
    cfg = config or AppConfig()
    session = boot(cfg, **boot_kwargs)
    # The writer_lock lives on the session (passed through the constructor
    # by _boot_tail for live mode). Non-live sessions have None.
    writer_lock = getattr(session, "_writer_lock", None)
    return RuntimeContext(
        config=cfg,
        session=session,
        engine=session.engine,
        strategy_engine=cast("ReactiveStrategyEngine | None", session.strategy_engine),
        bus=session.bus,
        broker=session.broker,
        writer_lock=writer_lock,
    )


__all__ = ["RuntimeContext", "boot", "boot_context"]
