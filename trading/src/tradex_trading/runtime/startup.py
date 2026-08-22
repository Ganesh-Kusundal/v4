"""Runtime boot — composition root for the v4 trading platform.

This is the ONLY place that wires all components together.
Fail-closed: any error during boot prevents session creation.

``boot`` also wires the extensions auto-discovery contract: every strategy
discovered in ``strategy/extensions`` is registered into a
``ReactiveStrategyEngine``, and every discovered scanner definition is bound
into the session's ``ScannerService`` (via ``scanner_definitions``).
"""

from __future__ import annotations

import logging
import atexit
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
    """

    config: AppConfig
    session: TradingSession
    engine: ExecutionEngine
    strategy_engine: object | None
    bus: ReactiveBus | ThreadSafeReactiveBus
    broker: Any

    def close(self) -> None:
        """Stop the session and release runtime resources."""
        self.session.stop()
        if _ACTIVE_WRITER_LOCK is not None:
            try:
                _ACTIVE_WRITER_LOCK.release()
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
        close_fn = getattr(self.broker, "close", None)
        if close_fn is not None:
            close_fn()


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
    discovered scanner definitions are bound into the session's ScannerService.

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

    # 5. Create risk manager
    risk_manager = RiskManager(
        max_order_value=cfg.risk.max_order_value,
        max_position_value=cfg.risk.max_position_value,
        max_orders_per_minute=cfg.risk.max_orders_per_minute,
    )

    # 6. Create execution engine
    engine = ExecutionEngine(
        bus=bus, fill_source=fill_source, risk_manager=risk_manager, metrics=metrics,
        idempotency_guard=guard, fee_calculator=fee_calculator,
    )
    # Bind the OMS cache as the position source so ``max_position_value`` is
    # enforced against live cumulative exposure (qty * avg_price + incoming).
    risk_manager.set_positions_provider(engine.cache.all_positions)
    engine.kill_switch = cfg.kill_switch_default

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
    # session.stream.subscribe_orders/positions reaches the broker WebSocket
    # instead of falling back to a stub. Paper/backtest brokers expose no
    # backend; any wiring failure degrades to the existing bus fallback.
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

    # 7c. Scanner engine — bind the market provider so the session's
    # ScannerService can run every auto-discovered extension scanner.
    # Backtest/replay modes scan the local parquet datalake (offline, full
    # Nifty universe) instead of the broker; paper/live keep live data.
    scanner_engine: Any = None
    if wire_strategies:
        if cfg.mode in ("backtest", "replay"):
            from tradex_trading.datalake.market_provider import ParquetMarketProvider
            scanner_market: Any = ParquetMarketProvider()
        else:
            scanner_market = broker
        scanner_engine = ScannerEngine(market=scanner_market)

    # 7d. Backtest loader — backtest/replay modes expose an offline datalake
    # backtest tool on the session: ``session.backtest.load()``/``.run()``
    # assemble ``BacktestEngine`` inputs from the parquet store (multi-symbol,
    # no broker). Paper/live keep None (live data paths instead).
    backtest_loader: Any = None
    if cfg.mode in ("backtest", "replay"):
        from tradex_trading.datalake.backtest_loader import ParquetBacktestLoader
        backtest_loader = ParquetBacktestLoader()

    # 8. Create session — strategies registered, scanners bound into the
    # ScannerService (definitions) so ``session.scanner.run_all()`` works.
    # Live mode additionally wires the market feed and the daily master
    # refresh scheduler HERE (moved from ``TradingSession.live`` — the
    # composition root owns all wiring [REF-6]). The scheduler is started
    # only after the session is READY.
    market_feed: Any = None
    master_scheduler: Any = None
    if cfg.mode == "live":
        from tradex_trading.runtime.master_lifecycle import (
            InstrumentRefreshScheduler,
            MasterLoader,
        )
        from tradex_trading.runtime.market_feed import MarketFeed

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

    # 9. Start session, then the refresh daemon (started last so nothing
    # after it can strand it).
    session.start()

    # 9b. Live restart reconciliation (R1): persisted local state was restored
    # pre-start; now refresh it against broker truth so fills/cancels that
    # happened while the process was down are picked up, and log any drift.
    if cfg.mode == "live":
        try:
            book = broker.get_orderbook()
        except Exception as exc:  # noqa: BLE001 — reconcile is best-effort
            log.warning("startup order-book reconcile unavailable: %s", exc)
        else:
            drifts = engine.reconcile(broker_orders=book)
            for row in book:
                current = engine.cache.get_order(row.order_id.value)
                if current is not None and current.status != row.status:
                    engine.cache.update_order(row)
            if drifts:
                for item in drifts:
                    log.warning("startup drift: %s", item)

    if master_scheduler is not None:
        master_scheduler.start()

    log.info("Runtime context ready")
    return session


def boot_context(
    config: AppConfig | None = None,
    **boot_kwargs: Any,
) -> RuntimeContext:
    """Boot and return a full RuntimeContext for lifecycle management.

    Like ``boot`` but returns a ``RuntimeContext`` that bundles all components
    and provides a ``close()`` method for clean shutdown. Extra keyword
    arguments (e.g. ``bus=`` or ``broker=``) are forwarded to :func:`boot`.
    """
    cfg = config or AppConfig()
    session = boot(cfg, **boot_kwargs)
    return RuntimeContext(
        config=cfg,
        session=session,
        engine=session.engine,
        strategy_engine=session.strategy_engine,
        bus=session.bus,
        broker=session.broker,
    )


__all__ = ["RuntimeContext", "boot", "boot_context"]
