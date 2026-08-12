"""TradingSession — main entry point for the v4 trading platform.

Provides 6 services: market, trade, portfolio, stream, scanner, extension.
Lifecycle: NEW -> READY -> STOPPED.

Ported from v3 SDK session (WS-B, FDS 05 §5, D-8/D-9/D-15/D-16/D-17).
Capability-loud: services gate on ``BrokerCapabilities`` and raise typed
``SDKError`` subclasses (D-8).
"""

from __future__ import annotations

import logging
from collections.abc import Callable, Sequence
from datetime import date
from decimal import Decimal
from enum import StrEnum
from functools import cached_property
from typing import Any, cast

from tradex_domain import BrokerId, SessionStateError
from tradex_domain.capabilities import BrokerCapabilities
from tradex_domain.errors import CapabilityNotSupportedError, OrderRejectedError
from tradex_domain.instruments import Equity, Future, Index, Instrument, Option
from tradex_domain.protocols import BrokerAdapter
from tradex_domain.strategy import ScannerDefinition
from tradex_domain.value_objects import Price

from tradex_trading.config.schema import AppConfig
from tradex_trading.execution.engine import ExecutionEngine
from tradex_trading.execution.fees import FeeCalculator
from tradex_trading.execution.trading_cache import TradingCache
from tradex_trading.reactive.bus import ReactiveBus
from tradex_trading.reactive.thread_safe_bus import ThreadSafeReactiveBus
from tradex_trading.sdk.services import (
    EdisStatus,
    ExtensionService,
    KillSwitchResult,
    MarketService,
    OrderResult,
    PortfolioService,
    ScannerService,
    StreamService,
    TpinResult,
    TradeService,
    _as_order_id,
    _broker_capabilities,
)
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
    ) -> None:
        self._broker = broker
        self._bus = bus
        self._engine = engine
        self._cache = cache
        self._broker_id = broker_id
        self._mode = mode
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
        self._market_feed: Any | None = None
        #: Daily instrument-master refresh daemon (live brokers only). Started
        #: by ``TradingSession.live()`` when the broker carries a cached master
        #: loader; stopped here so a long-running session re-downloads the
        #: master (new option series post monthly expiry) without leaking.
        self._master_scheduler: Any | None = None

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

    # --- 7 Services ---

    @cached_property
    def market(self) -> MarketService:
        """MarketService — quotes, depth, history, batch, search, chains."""
        self._check_ready()
        return MarketService(self._broker, _broker_capabilities(self._broker))

    @cached_property
    def trade(self) -> TradeService:
        """TradeService — order submission, cancellation, modification."""
        self._check_ready()
        caps = _broker_capabilities(self._broker)
        gate = self._make_order_gate()
        return TradeService(self._engine, self._bus, self._broker, caps, order_gate=gate)

    @cached_property
    def portfolio(self) -> PortfolioService:
        """PortfolioService — positions, holdings, account, portfolio."""
        self._check_ready()
        return PortfolioService(self._broker, self._cache)

    @property
    def account(self) -> PortfolioService:
        """Account/portfolio facade — positions, holdings, funds.

        Alias of :attr:`portfolio` so ``session.account.positions()``,
        ``session.account.funds()`` and ``session.account.holdings()`` read
        the same surface as ``session.portfolio.*`` (v3-style convenience;
        ``session.portfolio.account()`` remains the canonical account
        snapshot call).
        """
        return self.portfolio

    @cached_property
    def stream(self) -> StreamService:
        """StreamService — reactive market data, order, and position streams."""
        self._check_ready()
        caps = _broker_capabilities(self._broker)
        return StreamService(
            self._bus, self._subscriptions, caps, backend=self._stream_backend,
        )

    @cached_property
    def scanner(self) -> ScannerService:
        """ScannerService — scanner definitions and results."""
        self._check_ready()
        return ScannerService(
            self._scanner_engine, definitions=self._scanner_definitions
        )

    @cached_property
    def extension(self) -> ExtensionService:
        """ExtensionService — broker-specific extensions."""
        self._check_ready()
        caps = _broker_capabilities(self._broker)
        gate = self._make_order_gate()
        return ExtensionService(self._broker, caps, order_gate=gate)

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
        from tradex_brokers.paper.adapter import PaperBroker

        from tradex_trading.execution.fill_sources import PaperFillSource
        from tradex_trading.execution.slippage import PercentageSlippageModel

        cfg = config or AppConfig()
        slippage_model: object | None = None
        if cfg.execution.slippage_bps is not None:
            slippage_model = PercentageSlippageModel(
                pct=cfg.execution.slippage_bps / Decimal("10000"),
            )
        fee_calculator = (
            FeeCalculator() if cfg.execution.fees_enabled else None
        )
        broker = PaperBroker()
        _bus = bus or ReactiveBus()
        _engine = ExecutionEngine(
            bus=_bus,
            fill_source=PaperFillSource(slippage_model=slippage_model),
            fee_calculator=fee_calculator,
        )
        session = cls(
            broker=broker,
            bus=_bus,
            engine=_engine,
            # Mirrors ``runtime.startup.boot``: the session shares the engine's
            # OMS cache so fills/positions land where ``portfolio.positions()``
            # and the HTTP API read them (a second cache would silently show
            # empty positions/orders).
            cache=_engine.cache,
            broker_id=BrokerId(broker_id) if broker_id else BrokerId.PAPER,
        )
        # Mirrors ``TradingSession.live()`` and ``runtime.startup.boot``: every
        # factory returns a READY session so its services are usable without an
        # explicit ``start()``. The raw constructor stays NEW by design.
        session.start()
        return session

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

        from tradex_trading.execution.fill_sources import BrokerFillSource
        from tradex_trading.reactive.thread_safe_bus import ThreadSafeReactiveBus
        from tradex_trading.runtime.live import build_broker_from_env

        broker = build_broker_from_env(broker_id.value)
        # Thread-safe bus: live ticks arrive on the broker feed thread while
        # engine workers and API callers publish concurrently — serializing
        # publishes (RLock) prevents Subject delivery from interleaving.
        _bus = bus or ThreadSafeReactiveBus()
        _engine = ExecutionEngine(bus=_bus, fill_source=BrokerFillSource(broker=broker))
        from tradex_trading.runtime.market_feed import MarketFeed

        # Load the instrument master (registry) so feed subscriptions resolve
        # provider keys. Mirrors ``runtime.startup.boot`` which connects the
        # broker before starting the session.
        broker.connect()
        # Bind the order/portfolio stream backend so
        # session.stream.subscribe_orders/positions reaches the broker
        # WebSocket. Degrades to None if the broker exposes no backend.
        stream_backend = None
        try:
            sb = getattr(broker, "stream_backend", None)
            if callable(sb):
                stream_backend = sb()
        except Exception:  # noqa: BLE001 – best-effort wiring
            stream_backend = None

        # Live fill bridge: translate broker order-stream updates into bus
        # OrderFilled events so live fills reach the OMS (HIGH-4). Best-effort
        # — no bridge, no live fills, but boot never fails on it.
        fill_bridge = None
        if stream_backend is not None and hasattr(
            stream_backend, "subscribe_orders"
        ):
            try:
                from tradex_trading.sdk.live_fill_bridge import LiveFillBridge

                fill_bridge = LiveFillBridge(
                    _bus, _engine, stream_backend.subscribe_orders,
                )
            except Exception:  # noqa: BLE001 – best-effort wiring
                log.warning("live fill bridge unavailable: %s", exc_info=True)
                fill_bridge = None

        session = cls(
            broker=cast(BrokerAdapter, broker),
            bus=_bus,
            engine=_engine,
            # Mirrors ``runtime.startup.boot``: share the engine's OMS cache.
            cache=_engine.cache,
            broker_id=broker_id,
            stream_backend=stream_backend,
            fill_bridge=fill_bridge,
        )
        session._market_feed = MarketFeed(broker=broker, bus=_bus)
        # Mirrors ``runtime.startup.boot``: a live session must be READY for
        # its services (market/stream/trade) to be accessible.
        session.start()
        # Daily master refresh (v3 parity): only when the broker carries a
        # cached master loader. Started last so nothing after it can strand the
        # daemon. Best-effort — the warm load already ran on ``broker.connect()``;
        # failures are counted, never raised. The ``isinstance`` guard keeps
        # test doubles (MagicMock brokers) out.
        from tradex_trading.runtime.master_lifecycle import InstrumentRefreshScheduler, MasterLoader

        loader = getattr(broker, "master_loader", None)
        refresh_hook = getattr(broker, "ensure_master_fresh", None)
        if isinstance(loader, MasterLoader) and callable(refresh_hook):
            scheduler = InstrumentRefreshScheduler(broker_id.value.lower(), refresh_hook)
            scheduler.start()
            session._master_scheduler = scheduler
        return session

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


__all__ = [
    "EdisStatus",
    "ExtensionService",
    "KillSwitchResult",
    "MarketService",
    "PortfolioService",
    "ScannerService",
    "SessionState",
    "StreamService",
    "OrderResult",
    "TpinResult",
    "TradeService",
    "TradingSession",
    "_as_order_id",
    "_broker_capabilities",
]
