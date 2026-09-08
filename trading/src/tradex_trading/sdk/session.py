"""TradingSession — main entry point for the v4 trading platform.

Lifecycle: NEW -> READY -> STOPPED.
Consumers access engine, broker, bus, cache directly (service layer removed).

Ported from v3 SDK session (WS-B, FDS 05 §5, D-8/D-9/D-15/D-16/D-17).
"""

from __future__ import annotations

import logging
from collections.abc import Callable, Sequence
from dataclasses import replace
from datetime import date
from decimal import Decimal
from enum import StrEnum
from typing import Any

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

from tradex_trading.config.schema import AppConfig
from tradex_trading.execution.engine import ExecutionEngine
from tradex_trading.execution.trading_cache import TradingCache
from tradex_trading.reactive.bus import ReactiveBus
from tradex_trading.reactive.thread_safe_bus import ThreadSafeReactiveBus
from tradex_trading.sdk.streaming import StreamSubscription

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
        stream_backend: object | None = None,
        backtest_loader: object | None = None,
        live_orders_enabled: bool = True,
        fill_bridge: object | None = None,
        market_feed: object | None = None,
        master_scheduler: object | None = None,
        mark_to_market: object | None = None,
        metrics: object | None = None,
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
        self._stream_backend = stream_backend
        self._backtest_loader = backtest_loader
        self._live_orders_enabled = live_orders_enabled
        #: LiveFillBridge translating broker order-stream updates into bus
        #: OrderFilled events (live fills reaching the OMS — HIGH-4).
        self._fill_bridge = fill_bridge
        #: Wired in by the composition path (``live()``/``boot``) via the
        #: constructor — no post-init private mutation [REF-5].
        self._market_feed = market_feed
        #: Daily instrument-master refresh daemon (live brokers only). Passed
        #: in by ``TradingSession.live()`` when the broker carries a cached
        #: master loader; stopped here so a long-running session re-downloads
        #: the master (new option series post monthly expiry) without leaking.
        self._master_scheduler = master_scheduler
        #: Quote-driven position marking service. The composition root owns
        #: construction; the session owns lifecycle teardown.
        self._mark_to_market = mark_to_market

    def start(self) -> None:
        """Transition to READY state. Idempotent: no-op if already READY."""
        if self._state == SessionState.READY:
            return  # already ready, no-op
        if self._state not in (SessionState.NEW,):
            raise SessionStateError(
                f"Cannot start session in {self._state} state (must be NEW)"
            )
        sid = getattr(self, '_session_id', id(self))
        log.info("Session %s starting", sid)
        self._state = SessionState.READY

    def stop(self) -> None:
        """Transition to STOPPED state. Dispose all subscriptions.

        Idempotent: calling stop() on an already-stopped session is a no-op.
        """
        if self._state == SessionState.STOPPED:
            return  # already stopped
        if self._state == SessionState.NEW:
            return  # never started, nothing to stop
        for sub in self._subscriptions:
            sub.cancel()
        self._subscriptions.clear()
        if self._market_feed is not None:
            try:
                self._market_feed.stop()
            except Exception:  # pragma: no cover – defensive teardown
                log.warning("market feed stop failed", exc_info=True)
        if self._mark_to_market is not None:
            try:
                self._mark_to_market.close()  # type: ignore[attr-defined]
            except Exception:  # pragma: no cover – defensive teardown
                log.warning("mark-to-market stop failed", exc_info=True)
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
                self._master_scheduler.stop()
            except Exception:  # pragma: no cover – defensive teardown
                log.warning("master refresh scheduler stop failed", exc_info=True)
            self._master_scheduler = None
        # Tear down the broker's WebSocket sockets (market/depth/order feeds).
        # Nothing else ever closes them, so without this the daemon receive
        # loops and the reconnect machinery stay alive — minting fresh tokens
        # forever — after the session is stopped.
        close_fn = getattr(self._broker, "close", None)
        if callable(close_fn):
            try:
                close_fn()
            except Exception:  # pragma: no cover – defensive teardown
                log.warning("broker close failed", exc_info=True)
        self._state = SessionState.STOPPED
        sid = getattr(self, '_session_id', id(self))
        log.info("Session %s stopped", sid)

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
    def state(self) -> SessionState:
        """Current session state."""
        return self._state

    @property
    def broker_id(self) -> BrokerId:
        """Broker identifier."""
        return self._broker_id

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
    def backtest(self) -> object | None:
        """Offline datalake backtest loader (backtest/replay modes only).

        Returns a :class:`ParquetBacktestLoader` exposing ``load(...)``
        (flat candle list for ``BacktestEngine.run``) and ``run(strategy,
        ...)`` (one-shot backtest) over the local parquet datalake. ``None``
        in paper/live modes, which trade live data instead.
        """
        return self._backtest_loader

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

        Returns
        -------
        TradingSession
            A session configured for paper trading.
        """
        # Thin wrapper over the single composition root [REF-6]: the broker
        # is injected so paper() keeps its always-simulated semantics even
        # for a non-PAPER label; everything else is boot()'s wiring.
        from tradex_brokers.paper.adapter import PaperBroker

        from tradex_trading.runtime.startup import boot

        cfg = config or AppConfig()
        cfg = replace(
            cfg,
            mode="paper",
            broker_id=BrokerId(broker_id) if broker_id else BrokerId.PAPER,
        )
        # Historical minimal component set: no reactive strategy/scanner wiring.
        return boot(cfg, bus=bus, broker=PaperBroker(), wire_strategies=False)

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
        self._market_feed.start(instruments)

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
        from tradex_trading.runtime.startup import boot

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
            self.stop()
        except Exception:
            pass

__all__ = [
    "SessionState",
    "TradingSession",
]
