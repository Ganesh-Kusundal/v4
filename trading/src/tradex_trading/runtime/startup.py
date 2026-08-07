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
from dataclasses import dataclass
from typing import Any

from tradex_brokers import BrokerFactory, DhanBroker, PaperBroker, UpstoxBroker
from tradex_domain import BrokerId

from tradex_trading.config.schema import AppConfig
from tradex_trading.execution.engine import ExecutionEngine, RiskManager
from tradex_trading.execution.fill_sources import (
    BrokerFillSource,
    PaperFillSource,
    SimulatedFillSource,
)
from tradex_trading.reactive.bus import ReactiveBus
from tradex_trading.runtime.metrics import MetricsRegistry
from tradex_trading.sdk.session import TradingSession
from tradex_trading.strategy.core.engine import ReactiveStrategyEngine
from tradex_trading.strategy.core.scanner import ScannerEngine
from tradex_trading.strategy.extensions import all_scanners, all_strategies

log = logging.getLogger(__name__)


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
    bus: ReactiveBus
    broker: Any

    def close(self) -> None:
        """Stop the session and release runtime resources."""
        self.session.stop()
        if hasattr(self, "engine") and self.engine is not None:
            try:
                self.engine.shutdown()
            except Exception as exc:  # pragma: no cover
                log.error("Error shutting down engine: %s", exc)
        if self.strategy_engine is not None:
            try:
                self.strategy_engine.dispose_all()  # type: ignore[union-attr]
            except Exception as exc:  # pragma: no cover
                log.error("Error disposing strategy engine: %s", exc)
        close_fn = getattr(self.broker, "close", None)
        if close_fn is not None:
            close_fn()


def boot(config: AppConfig | None = None) -> TradingSession:
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

    # 1. Create broker via BrokerFactory
    broker = BrokerFactory.create(cfg.broker_id)

    # 2. Create metrics registry
    metrics = MetricsRegistry()

    # 3. Create reactive bus
    bus = ReactiveBus(metrics=metrics)

    # 4. Create fill source based on mode
    fill_source: Any
    if cfg.mode == "paper":
        fill_source = PaperFillSource()
    elif cfg.mode == "backtest":
        fill_source = SimulatedFillSource()
    elif cfg.mode == "live":
        fill_source = BrokerFillSource(broker)
    elif cfg.mode == "replay":
        fill_source = SimulatedFillSource()
    else:
        raise ValueError(f"unknown mode: {cfg.mode}")

    # 5. Create risk manager
    risk_manager = RiskManager(
        max_order_value=cfg.risk.max_order_value,
        max_position_value=cfg.risk.max_position_value,
        max_orders_per_minute=cfg.risk.max_orders_per_minute,
    )

    # 6. Create execution engine
    engine = ExecutionEngine(
        bus=bus, fill_source=fill_source, risk_manager=risk_manager, metrics=metrics,
    )
    engine.kill_switch = cfg.kill_switch_default

    # 6b. Strategy engine — register every auto-discovered extension strategy
    # so user strategies (strategy/extensions) run without touching core.
    # Discovery already isinstance-filters against the Strategy protocol, so
    # registration cannot realistically fail; any error still aborts boot
    # (fail-closed — nothing is swallowed).
    strategy_engine = ReactiveStrategyEngine(bus)
    for strategy in all_strategies:
        strategy_engine.register(strategy)

    # 7. Connect broker (loads instruments/registry for live brokers)
    broker.connect()

    # 7b. Bind the order/portfolio stream backend (live brokers only) so
    # session.stream.subscribe_orders/positions reaches the broker WebSocket
    # instead of falling back to a stub. Paper/backtest brokers expose no
    # backend; any wiring failure degrades to the existing bus fallback.
    stream_backend = None
    if cfg.mode == "live":
        try:
            sb = getattr(broker, "stream_backend", None)
            if callable(sb):
                stream_backend = sb()
        except Exception as exc:  # noqa: BLE001 – degrade, don't fail boot
            log.warning("stream backend unavailable at boot: %s", exc)

    # 7c. Scanner engine — bind the market provider so the session's
    # ScannerService can run every auto-discovered extension scanner.
    scanner_engine = ScannerEngine(market=broker)

    # 8. Create session — strategies registered, scanners bound into the
    # ScannerService (definitions) so ``session.scanner.run_all()`` works.
    session = TradingSession(
        broker=broker,
        bus=bus,
        engine=engine,
        cache=engine.cache,
        broker_id=cfg.broker_id,
        mode=cfg.mode,
        scanner_engine=scanner_engine,
        scanner_definitions=all_scanners,
        strategy_engine=strategy_engine,
        stream_backend=stream_backend,
    )

    # 9. Start session
    session.start()

    log.info("Runtime context ready")
    return session


def boot_context(config: AppConfig | None = None) -> RuntimeContext:
    """Boot and return a full RuntimeContext for lifecycle management.

    Like ``boot`` but returns a ``RuntimeContext`` that bundles all components
    and provides a ``close()`` method for clean shutdown.
    """
    cfg = config or AppConfig()
    session = boot(cfg)
    return RuntimeContext(
        config=cfg,
        session=session,
        engine=session.engine,
        strategy_engine=session.strategy_engine,
        bus=session.bus,
        broker=session.broker,
    )


__all__ = ["RuntimeContext", "boot", "boot_context"]
