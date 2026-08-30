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
from dataclasses import dataclass
from decimal import Decimal
from pathlib import Path
from typing import Any, cast

from tradex_brokers import DhanBroker, PaperBroker, UpstoxBroker
from tradex_domain import BrokerId

from tradex_trading.config.schema import AppConfig
from tradex_trading.execution.engine import ExecutionEngine, RiskManager
from tradex_trading.execution.fees import FeeCalculator
from tradex_trading.execution.fill_sources import (
    BrokerFillSource,
    PaperFillSource,
    SimulatedFillSource,
)
from tradex_trading.execution.slippage import PercentageSlippageModel
from tradex_trading.reactive.bus import ReactiveBus
from tradex_trading.reactive.thread_safe_bus import ThreadSafeReactiveBus
from tradex_trading.runtime.metrics import MetricsRegistry
from tradex_trading.sdk.session import TradingSession
from tradex_trading.strategy.core.engine import ReactiveStrategyEngine
from tradex_trading.strategy.core.scanner import ScannerEngine
from tradex_trading.strategy.extensions import all_scanners, all_strategies

log = logging.getLogger(__name__)

_ACTIVE_WRITER_LOCK: Any = None


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

    M7: ``writer_lock`` is the local lock this context acquired in the
    live boot path. ``close()`` releases it directly instead of going
    through a module global, so two RuntimeContexts don't trample
    each other's writer lock.
    """

    config: AppConfig
    session: TradingSession
    engine: ExecutionEngine
    strategy_engine: object | None
    bus: ReactiveBus | ThreadSafeReactiveBus
    broker: Any
    writer_lock: Any = None

    def close(self) -> None:
        """Stop the session and release runtime resources.

        M4: ``broker.close()`` is invoked exactly once — by
        ``session.stop()`` below. The previous second call here
        relied on broker idempotency, which is not part of the
        ``BaseBroker`` contract. Removing the duplicate makes the
        teardown order explicit and lets a non-idempotent broker
        surface a real error.

        M7: releases the *local* ``self.writer_lock`` (not the
        module global) so a second RuntimeContext that overwrote
        the global cannot accidentally release the first context's
        lock.
        """
        self.session.stop()
        if self.writer_lock is not None:
            try:
                self.writer_lock.release()
            except Exception:  # pragma: no cover
                log.warning("writer lock release failed", exc_info=True)
        if hasattr(self, "engine") and self.engine is not None:
            try:
                self.engine.shutdown()
            except Exception as exc:  # pragma: no cover
                log.error("Error shutting down engine: %s", exc)
        if self.strategy_engine is not None:
            try:
                self.strategy_engine.dispose_all()  # type: ignore[attr-defined]
            except Exception as exc:  # pragma: no cover
                log.error("Error disposing strategy engine: %s", exc)


def boot(
    config: AppConfig | None = None,
    *,
    bus: ReactiveBus | ThreadSafeReactiveBus | None = None,
    broker: Any | None = None,
    wire_strategies: bool = True,
) -> TradingSession:
    """Fail-closed boot: compose all components into a ready TradingSession.

    This is the composition root — the ONLY place that wires everything together.
    Extension strategies are registered into a ``ReactiveStrategyEngine`` and
    discovered scanner definitions are bound into the session's scanner engine.

    Parameters
    ----------
    config : AppConfig | None
        Application configuration. If None, uses defaults (paper mode).

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
    log.info("Booting runtime context...")

    # 0. Safety gates
    valid_modes = {"paper", "backtest", "replay", "live"}
    if cfg.mode not in valid_modes:
        raise ValueError(
            f"unknown mode: {cfg.mode!r}, must be one of {sorted(valid_modes)}"
        )
    if cfg.mode == "live" and cfg.broker_id == BrokerId.PAPER:
        raise ValueError("live mode requires a non-paper broker")
    if cfg.mode == "live" and not cfg.live_enabled:
        raise ValueError("live mode requires live_enabled=true in config")

    # 1. Create broker. Live brokers bind a real transport via the standard
    # interface (build_broker_from_env); paper/backtest/replay construct the
    # transport-less adapter directly (paper is transport-less by design).
    # An injected broker (seam for tests and the SDK factories) wins.
    if broker is None:
        if cfg.mode == "live":
            from tradex_trading.runtime.live import build_broker_from_env
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
            pct=cfg.execution.slippage_bps / Decimal("10000"),
        )
    fee_calculator: FeeCalculator | None = (
        FeeCalculator() if cfg.execution.fees_enabled else None
    )

    # 4. Create fill source based on mode
    fill_source: Any
    if cfg.mode == "paper":
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
    guard: Any = None
    order_store: Any = None
    if cfg.persistence.path:
        from tradex_trading.execution.sqlite_store import (
            SQLiteIdempotencyGuard,
            SQLiteOrderStore,
        )
        guard = SQLiteIdempotencyGuard(cfg.persistence.path)
        order_store = SQLiteOrderStore(cfg.persistence.path)

    # 4c. Single-writer guard (R2) — live only. Rate limiters are per-process;
    # two live writers on one account can jointly breach provider limits.
    writer_lock: Any = None
    if cfg.mode == "live":
        from tradex_trading.runtime.writer_lock import SingleWriterLock

        writer_lock = SingleWriterLock(
            Path("runtime/live") / f"{cfg.broker_id.value.lower()}.writer.lock"
        )
        writer_lock.acquire()  # fail-closed if another live process is running
        global _ACTIVE_WRITER_LOCK
        _ACTIVE_WRITER_LOCK = writer_lock
        atexit.register(writer_lock.release)  # stale-PID auto-clear covers crashes
        try:
            return _boot_tail(
                cfg, bus, broker, wire_strategies, writer_lock,
                fill_source=fill_source, metrics=metrics, guard=guard,
                fee_calculator=fee_calculator, order_store=order_store,
            )
        except BaseException:
            # C3: best-effort rollback so a failed live boot never
            # leaves a connected transport, a stranded writer lockfile,
            # or a subscribed bus.
            _safe_teardown(session=None, broker=broker, bus=bus, writer_lock=writer_lock)
            raise

    # C3: non-live branch also rolls back. The session is built inside
    # _boot_tail, so we pass session=None — the helper tolerates that
    # and only disconnects the broker and disposes the bus. (There is
    # no writer lock to release in non-live mode.)
    try:
        return _boot_tail(
            cfg, bus, broker, wire_strategies, None,
            fill_source=fill_source, metrics=metrics, guard=guard,
            fee_calculator=fee_calculator, order_store=order_store,
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

    Disconnects the broker, disposes the bus, stops the session (if
    built), and releases the writer lock (if any). Every call is
    wrapped in its own try/except so one failure does not mask another.
    Used by both the live and non-live branches of :func:`boot`.
    """
    disconnect = getattr(broker, "disconnect", None)
    if callable(disconnect):
        try:
            disconnect()
        except Exception:  # noqa: BLE001 — best-effort rollback
            log.warning("broker disconnect during boot rollback failed", exc_info=True)
    bus_dispose = getattr(bus, "dispose", None)
    if callable(bus_dispose):
        try:
            bus_dispose()
        except Exception:  # noqa: BLE001 — best-effort rollback
            log.warning("bus dispose during boot rollback failed", exc_info=True)
    if session is not None:
        stop = getattr(session, "stop", None)
        if callable(stop):
            try:
                stop()
            except Exception:  # noqa: BLE001 — best-effort rollback
                log.warning("session stop during boot rollback failed", exc_info=True)
    if writer_lock is not None:
        try:
            writer_lock.release()
        except Exception:  # noqa: BLE001 — best-effort rollback
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
) -> TradingSession:
    """Steps 5→end of :func:`boot`, extracted so the live path can be
    rollback-wrapped. The body is the original ``boot()`` logic unchanged.
    """
    # 5. Create risk manager
    risk_manager = RiskManager(
        max_order_value=cfg.risk.max_order_value,
        max_position_value=cfg.risk.max_position_value,
        max_orders_per_minute=cfg.risk.max_orders_per_minute,
        reject_unknown_market_value=cfg.risk.reject_unknown_market_value,
        max_daily_loss_amt=cfg.risk.max_daily_loss_amt,
        max_drawdown_pct=cfg.risk.max_drawdown_pct,
    )

    # 6. Create execution engine
    engine = ExecutionEngine(
        bus=bus, fill_source=fill_source, risk_manager=risk_manager, metrics=metrics,
        idempotency_guard=guard, fee_calculator=fee_calculator,
    )
    # Bind the OMS cache as the position source so ``max_position_value`` is
    # enforced against live cumulative exposure (qty * avg_price + incoming).
    risk_manager.set_positions_provider(engine.cache.all_positions)
    # C1 follow-up: bind the cash provider from config when set. Paper/live
    # sessions without a cash_provider see the gate off (backward-compat);
    # backtest/replay own their own CashLedger and bypass the engine gate.
    if cfg.risk.cash_provider is not None and cfg.mode in ("paper", "live"):
        risk_manager.bind_cash_provider(cfg.risk.cash_provider)
    engine.kill_switch = cfg.kill_switch_default

    # H7: pipeline errors must not be silent. The engine publishes
    # ``ErrorOccurred`` on the bus (engine.py:517, 524, 533); without a
    # subscriber those events are dropped. Wire a logger + counter so
    # operators see pipeline failures and the runtime exposes a metric.
    if metrics is not None:
        errors_total = metrics.counter("engine.errors.total")
        from tradex_domain.events import ErrorOccurred
        def _on_error(event):  # noqa: ANN001
            log.error("pipeline error: %s", event.error)
            errors_total.inc()
        # of_type() filters the bus stream to ErrorOccurred only;
        # the bus.subscribe() raw form would receive every event.
        bus.of_type(ErrorOccurred).subscribe(on_next=_on_error)

    # 6a. Order durability (R1) — restore persisted orders into the OMS cache
    # BEFORE the session starts, then mirror every lifecycle event into the
    # store. Subscriptions die with bus.dispose() on session.stop().
    if order_store is not None:
        from tradex_trading.execution.order_persistence import attach_order_persistence

        order_store.load_into(engine.cache)
        attach_order_persistence(bus, engine.cache, order_store)

    # 6b. Strategy engine — register every auto-discovered extension strategy
    # so user strategies (strategy/extensions) run without touching core.
    # Discovery already isinstance-filters against the Strategy protocol, so
    # registration cannot realistically fail; any error still aborts boot
    # (fail-closed — nothing is swallowed).
    strategy_engine: Any = None
    if wire_strategies:
        strategy_engine = ReactiveStrategyEngine(
            bus, fill_reference=cfg.execution.fill_reference,
        )
        for strategy in all_strategies:
            strategy_engine.register(strategy)

    # 7. Connect broker (loads instruments/registry for live brokers)
    broker.connect()

    # 7b. Bind the order/portfolio stream backend (live brokers only) so
    # callers using session.bus for OrderPlaced/OrderCancelled/OrderModified
    # (and live WS backends) reach the broker WebSocket instead of falling
    # back to a stub. Paper/backtest brokers expose no backend; any wiring
    # failure degrades to the existing bus fallback.
    stream_backend = None
    fill_bridge: Any = None
    if cfg.mode == "live":
        try:
            sb = getattr(broker, "stream_backend", None)
            if callable(sb):
                stream_backend = sb()
        except Exception as exc:  # noqa: BLE001 – degrade, don't fail boot
            log.warning("stream backend unavailable at boot: %s", exc)
        # Live fill bridge: translate broker order-stream updates into bus
        # OrderFilled events so live fills reach the OMS (HIGH-4). Best-effort
        # — without it boot still succeeds, but live fills never apply.
        if stream_backend is not None and hasattr(
            stream_backend, "subscribe_orders"
        ):
            try:
                from tradex_trading.sdk.live_fill_bridge import (
                    LiveFillBridge,
                    TradeBookFillIdResolver,
                )

                # Stamp live delta fills with the broker's exchange trade ids
                # (Dhan ``tradeId`` from GET /trades) so equal-lot partials
                # dedup exactly instead of under-counting (parity review area
                # #10 — duplicate-event safety). Brokers without a REST trade
                # book fall back to the composite fingerprint.
                trade_book = getattr(broker, "trade_book", None)
                resolver = (
                    TradeBookFillIdResolver(trade_book)
                    if callable(trade_book) else None
                )
                fill_bridge = LiveFillBridge(
                    bus, engine, stream_backend.subscribe_orders,
                    trade_id_resolver=resolver,
                    unsubscribe=getattr(stream_backend, "unsubscribe", None),
                )
            except Exception as exc:  # noqa: BLE001 – degrade, don't fail boot
                log.warning("live fill bridge unavailable at boot: %s", exc)
                fill_bridge = None
        else:
            # Live mode with no order-stream backend: broker fills can never
            # reach the OMS (no OrderFilled events) — the local book will
            # silently diverge from the venue. Loud, but not fatal.
            log.warning(
                "LIVE MODE WITHOUT ORDER-STREAM BACKEND: broker order/fill "
                "updates will NOT reach the OMS; local order state will "
                "diverge from the venue until reconciliation runs."
            )

    # 7c. Scanner engine — bind the market provider so the session can run
    # every auto-discovered extension scanner. Backtest/replay modes scan
    # the local parquet datalake (offline, full Nifty universe) instead of
    # the broker; paper/live keep live data.
    scanner_engine: Any = None
    if wire_strategies:
        if cfg.mode in ("backtest", "replay"):
            from tradex_trading.datalake.market_provider import ParquetMarketProvider
            scanner_market: Any = ParquetMarketProvider()
        else:
            scanner_market = broker
        scanner_engine = ScannerEngine(market=scanner_market)
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
        from tradex_trading.datalake.backtest_loader import ParquetBacktestLoader
        backtest_loader = ParquetBacktestLoader()

    # 8. Create session — strategies registered, scanner engine bound
    # so ``session._scanner_engine.run(definition)`` works.
    # Live mode additionally wires the market feed and the daily master
    # refresh scheduler HERE (moved from ``TradingSession.live`` — the
    # composition root owns all wiring [REF-6]). The scheduler is started
    # only after the session is READY.
    market_feed: Any = None
    master_scheduler: Any = None
    if cfg.mode == "live":
        from tradex_trading.runtime.market_feed import MarketFeed
        from tradex_trading.runtime.master_lifecycle import (
            InstrumentRefreshScheduler,
            MasterLoader,
        )

        market_feed = MarketFeed(broker=broker, bus=bus)
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
        stream_backend=stream_backend,
        backtest_loader=backtest_loader,
        fill_bridge=fill_bridge,
        market_feed=market_feed,
        master_scheduler=master_scheduler,
        metrics=metrics,
    )

    # 8b. Live single-writer lock releases when the session stops (composition
    # root wraps stop so every teardown path — context manager, explicit
    # stop(), RuntimeContext.close() — clears the lockfile).
    if writer_lock is not None:
        _inner_stop = session.stop

        def _stop_and_release() -> None:
            _inner_stop()
            writer_lock.release()

        session.stop = _stop_and_release  # type: ignore[method-assign]

    # 9. Live restart reconciliation (R1) — run BEFORE session.start() so
    # a *new* critical drift never leaves the session in READY (C2).
    # The strategy engine is already subscribed at this point, but the
    # session services are gated on _check_ready(), so the strategy
    # cannot place an order until session.start() transitions to READY.
    # A pre-configured ``kill_switch_default=True`` is NOT a "new" trip;
    # the session still goes READY so the user can observe rejection.
    reconcile_tripped = False
    if cfg.mode == "live":
        reconcile_tripped = _run_startup_reconciliation(broker, engine)
        if reconcile_tripped:
            log.critical(
                "Refusing to start session: kill switch tripped during "
                "startup reconciliation. Inspect drift before clearing."
            )

    # 10. Start session, then the refresh daemon (started last so nothing
    # after it can strand it).
    if not reconcile_tripped:
        session.start()
    else:
        # Leave session in NEW; caller can stop() to release resources.
        # Mark the master scheduler's expected-state so it is not started.
        master_scheduler = None

    if master_scheduler is not None:
        master_scheduler.start()

    log.info("Runtime context ready")
    return session


def _run_startup_reconciliation(broker: Any, engine: Any) -> bool:
    """Live-mode startup reconciliation. Extracted so tests can call it directly.

    - Pulls the broker orderbook + positions.
    - Calls ``engine.reconcile`` (side-effect free).
    - Updates the OMS cache from the broker book (status refresh only).
    - Logs every drift; trips the kill switch on HIGH/CRITICAL drift so
      a diverged book never reaches the trading surface.

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
        return False
    drifts = engine.reconcile(
        broker_orders=book, broker_positions=broker_positions,
    )
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



def boot_context(
    config: AppConfig | None = None,
    **boot_kwargs: Any,
) -> RuntimeContext:
    """Boot and return a full RuntimeContext for lifecycle management.

    Like ``boot`` but returns a ``RuntimeContext`` that bundles all components
    and provides a ``close()`` method for clean shutdown. Extra keyword
    arguments (e.g. ``bus=`` or ``broker=``) are forwarded to :func:`boot`.

    M7: the returned ``RuntimeContext`` carries the writer_lock the
    live boot acquired (or None for non-live modes), so its ``close()``
    can release the right one without consulting the module global.
    """
    cfg = config or AppConfig()
    session = boot(cfg, **boot_kwargs)
    # Resolve the writer_lock this boot acquired: the live path stores
    # it in the module global; the non-live path returns None. The
    # snapshot is taken here so subsequent boots don't clobber it
    # between this assignment and RuntimeContext construction.
    writer_lock: Any = None
    if cfg.mode == "live" and _ACTIVE_WRITER_LOCK is not None:
        writer_lock = _ACTIVE_WRITER_LOCK
    return RuntimeContext(
        config=cfg,
        session=session,
        engine=session.engine,
        strategy_engine=session.strategy_engine,
        bus=session.bus,
        broker=session.broker,
        writer_lock=writer_lock,
    )


__all__ = ["RuntimeContext", "boot", "boot_context"]
