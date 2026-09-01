"""Unified Trading Session — single entry point for all trading modes.

One TradingSession class where only the data source changes between modes.
Uses the same event-sourcing pipeline for all modes:
    EventStore -> OrderBookActor -> CommandProcessor -> Projectors

Architecture:
    All modes share:
    - EventStore (SQLite-backed append-only log)
    - OrderBookActor (single-writer state owner)
    - CommandProcessor (idempotent command handler)
    - Projectors (read model derivation)

    Only the data source and fill mechanism change:
    - Live: Broker WebSocket, real-time fills via FillMatcher
    - Backtest: Historical data, simulated fills
    - Replay: Event log replay at configurable speed
    - Paper: Simulated broker, no real money
"""

from __future__ import annotations

import logging
import uuid
from dataclasses import dataclass
from datetime import UTC, datetime
from decimal import Decimal
from enum import StrEnum
from typing import Optional

from tradex_domain import OrderRequest
from tradex_trading.events.actor import CancelOrderCommand, OrderBookActor, PlaceOrderCommand
from tradex_trading.events.processor import CommandProcessor, CommandResult
from tradex_trading.events.projectors import OrderBookProjector, PositionProjector
from tradex_trading.events.recovery import SessionRecovery
from tradex_trading.events.risk_engine import RiskConfig, RiskEngine
from tradex_trading.events.store import EventStore

log = logging.getLogger(__name__)


# =============================================================================
# Session State
# =============================================================================


class SessionState(StrEnum):
    """Session lifecycle states."""

    NEW = "NEW"
    READY = "READY"
    STOPPED = "STOPPED"


# =============================================================================
# Configuration Dataclasses
# =============================================================================


@dataclass(frozen=True, slots=True)
class BrokerConfig:
    """Broker connection configuration for live/paper modes.

    Attributes:
        broker_id: Broker identifier ("dhan", "upstox", "paper").
        client_id: Broker API client ID.
        access_token: Broker API access token.
        api_key: Optional broker API key.
    """

    broker_id: str
    client_id: str
    access_token: str
    api_key: Optional[str] = None


@dataclass(frozen=True, slots=True)
class DataSourceConfig:
    """Mode-specific data source configuration.

    Attributes:
        type: Data source type ("broker", "historical", "replay", "simulated").
        broker: Broker config for live/paper modes.
        historical_path: Path to historical data for backtest/replay.
        replay_speed: Speed multiplier for replay mode (1.0 = real-time).
    """

    type: str
    broker: Optional[BrokerConfig] = None
    historical_path: Optional[str] = None
    replay_speed: Optional[float] = None

    def __post_init__(self) -> None:
        valid_types = {"broker", "historical", "replay", "simulated"}
        if self.type not in valid_types:
            raise ValueError(
                f"Invalid data source type: {self.type!r}. Must be one of {valid_types}"
            )


@dataclass(frozen=True, slots=True)
class SessionConfig:
    """Unified session configuration — one config for all modes.

    Attributes:
        session_id: Unique session identifier.
        mode: Trading mode ("live", "backtest", "replay", "paper").
        event_store_path: Path to SQLite event store database.
        risk_config: Risk engine configuration.
        data_source: Mode-specific data source configuration.
    """

    session_id: str
    mode: str
    event_store_path: str
    risk_config: RiskConfig
    data_source: DataSourceConfig

    def __post_init__(self) -> None:
        valid_modes = {"live", "backtest", "replay", "paper"}
        if self.mode not in valid_modes:
            raise ValueError(
                f"Invalid mode: {self.mode!r}. Must be one of {valid_modes}"
            )


# =============================================================================
# TradingSession
# =============================================================================


class TradingSession:
    """Single entry point for all trading modes.

    One class, unified pipeline — only the data source changes between modes.

    Lifecycle: NEW -> READY -> STOPPED.

    Usage:
        config = SessionConfig(session_id="...", mode="paper", ...)
        session = TradingSession(config)
        session.start()
        result = session.place_order(order_request)
        session.stop()
    """

    def __init__(self, config: SessionConfig) -> None:
        self._config = config
        self._state = SessionState.NEW

        # Core event-sourcing pipeline (shared across all modes)
        self._store = EventStore(config.event_store_path)
        self._actor = OrderBookActor(
            session_id=config.session_id,
            event_store=self._store,
        )
        self._processor = CommandProcessor(
            event_store=self._store,
            order_book=self._actor,
        )

        # Projectors for read models
        self._order_projector = OrderBookProjector()
        self._position_projector = PositionProjector()

        # Risk engine
        self._risk_engine = RiskEngine(config.risk_config)

        # Recovery
        self._recovery = SessionRecovery(
            event_store=self._store,
            order_book=self._actor,
            command_processor=self._processor,
        )

    # -------------------------------------------------------------------------
    # Lifecycle
    # -------------------------------------------------------------------------

    def start(self) -> None:
        """Start the session — recover state from event log.

        Transitions NEW -> READY. Idempotent: no-op if already READY.
        """
        if self._state == SessionState.READY:
            return
        if self._state not in (SessionState.NEW,):
            raise RuntimeError(f"Cannot start session in {self._state} state")

        # Recover state from event log
        result = self._recovery.recover()
        if not result.success:
            raise RuntimeError(f"Session recovery failed: {result.error}")

        # Rebuild projectors from recovered events
        self._rebuild_projectors()

        self._state = SessionState.READY
        log.info("Session %s started (mode=%s)", self._config.session_id, self._config.mode)

    def stop(self) -> None:
        """Stop the session — close resources.

        Transitions to STOPPED. Idempotent: no-op if already STOPPED.
        """
        if self._state == SessionState.STOPPED:
            return
        if self._state == SessionState.NEW:
            return  # Never started, nothing to stop

        self._store.close()
        self._state = SessionState.STOPPED
        log.info("Session %s stopped", self._config.session_id)

    def _check_running(self) -> None:
        """Verify session is in READY state."""
        if self._state != SessionState.READY:
            raise RuntimeError(
                f"Session is {self._state.value}, not running. Call start() first."
            )

    # -------------------------------------------------------------------------
    # Order Operations
    # -------------------------------------------------------------------------

    def place_order(self, request: OrderRequest) -> CommandResult:
        """Place an order through the unified pipeline.

        Args:
            request: The order request with instrument, side, quantity, price.

        Returns:
            CommandResult with success status and emitted events.
        """
        self._check_running()

        correlation_id = request.correlation_id or str(uuid.uuid4())
        event_time = datetime.now(UTC)

        command = PlaceOrderCommand(
            request=request,
            correlation_id=correlation_id,
            event_time=event_time,
        )

        result = self._processor.process(command)
        self._update_projectors(result.events)
        return result

    def cancel_order(self, order_id: str) -> CommandResult:
        """Cancel an existing order.

        Args:
            order_id: The internal order ID to cancel.

        Returns:
            CommandResult with success status and emitted events.
        """
        self._check_running()

        command = CancelOrderCommand(
            order_id=order_id,
            correlation_id=str(uuid.uuid4()),
            event_time=datetime.now(UTC),
        )

        result = self._processor.process(command)
        self._update_projectors(result.events)
        return result

    def apply_fill(
        self,
        order_id: str,
        cumulative_filled: Decimal,
        fill_price: Decimal,
        fill_id: Optional[str] = None,
    ) -> CommandResult:
        """Apply a fill to an order (used by live/replay modes).

        This is the unified entry point for fills — called by the FillMatcher
        in live mode, or by the historical simulator in backtest mode.

        Args:
            order_id: The internal order ID.
            cumulative_filled: Total filled quantity (cumulative).
            fill_price: The fill price.
            fill_id: Optional unique fill identifier.

        Returns:
            CommandResult with success status and emitted events.
        """
        self._check_running()

        from tradex_trading.events.actor import ApplyFillCommand

        command = ApplyFillCommand(
            order_id=order_id,
            cumulative_filled=cumulative_filled,
            fill_price=fill_price,
            fill_id=fill_id,
            correlation_id=str(uuid.uuid4()),
            event_time=datetime.now(UTC),
        )

        result = self._processor.process(command)
        self._update_projectors(result.events)
        return result

    def _update_projectors(self, events: list) -> None:
        """Update projectors with new events."""
        for event in events:
            self._order_projector.apply(event)
            self._position_projector.apply(event)

    # -------------------------------------------------------------------------
    # Read Models
    # -------------------------------------------------------------------------

    def get_orders(self) -> list[OrderView]:
        """Get all orders as read models.

        Returns:
            List of OrderView objects representing current order state.
        """
        self._check_running()
        return self._order_projector.get_all_orders()

    def get_positions(self) -> list[PositionView]:
        """Get all positions as read models.

        Returns:
            List of PositionView objects representing current position state.
        """
        self._check_running()
        return self._position_projector.get_all_positions()

    # -------------------------------------------------------------------------
    # Internal Helpers
    # -------------------------------------------------------------------------

    def _rebuild_projectors(self) -> None:
        """Rebuild projectors from the event log after recovery."""
        # Reset projectors
        self._order_projector = OrderBookProjector()
        self._position_projector = PositionProjector()

        # Replay all events through projectors
        events = self._store.read_all(self._config.session_id)
        for event in events:
            self._order_projector.apply(event)
            self._position_projector.apply(event)

    # -------------------------------------------------------------------------
    # Properties
    # -------------------------------------------------------------------------

    @property
    def state(self) -> str:
        """Current session state."""
        return self._state.value

    @property
    def mode(self) -> str:
        """Trading mode."""
        return self._config.mode

    @property
    def session_id(self) -> str:
        """Session identifier."""
        return self._config.session_id

    @property
    def data_source(self) -> DataSourceConfig:
        """Data source configuration."""
        return self._config.data_source

    @property
    def config(self) -> SessionConfig:
        """Session configuration."""
        return self._config


# =============================================================================
# Exports
# =============================================================================

__all__ = [
    "BrokerConfig",
    "DataSourceConfig",
    "SessionConfig",
    "SessionState",
    "TradingSession",
]
