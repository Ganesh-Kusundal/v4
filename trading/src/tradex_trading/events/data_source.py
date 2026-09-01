"""Data Source abstraction -- mode-specific market data and fill routing.

Each trading mode wires a different data source implementation:
    - broker (live): real-time market data and fills from broker WebSocket/API
    - historical (backtest): replayed historical OHLCV bars, simulated fills
    - simulated (paper): synthetic broker, no real money, instant fills
    - replay: event-log replay at configurable speed

The DataSource owns the fill pipeline: when an order is placed, the data
source either registers it with the broker (live) or simulates fills
(backtest/paper). Fills flow back into the session via the on_fill callback.
"""

from __future__ import annotations

import logging
from abc import ABC, abstractmethod
from dataclasses import dataclass
from datetime import UTC, datetime
from decimal import Decimal
from typing import TYPE_CHECKING, Callable, Optional

from tradex_trading.events.actor import ApplyFillCommand
from tradex_trading.events.fill_matcher import BrokerOrderUpdate, FillMatcher
from tradex_trading.events.processor import CommandProcessor, CommandResult

if TYPE_CHECKING:
    # Import only for static analysis to avoid a circular import:
    # session.py imports DataSource/FillCallback/create_data_source from here.
    from tradex_trading.events.session import BrokerConfig, DataSourceConfig

log = logging.getLogger(__name__)

# Callback invoked when a fill needs to be applied: (order_id, cumulative_filled, fill_price, fill_id).
# Returns the CommandResult of applying the fill (or None if the callback does
# not report one) so fill-source idempotency trackers can decide whether to advance.
FillCallback = Callable[[str, Decimal, Decimal, Optional[str]], Optional[CommandResult]]


class DataSource(ABC):
    """Abstract data source -- mode-specific market data and fill routing.

    Lifecycle:
        1. __init__() -- wire dependencies.
        2. start() -- connect / open data stream.
        3. [orders placed → fills routed back via on_fill callback]
        4. stop() -- disconnect / close.
    """

    def __init__(self, on_fill: FillCallback) -> None:
        self._on_fill = on_fill

    @abstractmethod
    def start(self) -> None:
        """Start the data source (connect to broker, open file, etc.)."""
        ...

    @abstractmethod
    def stop(self) -> None:
        """Stop the data source and release resources."""
        ...

    @abstractmethod
    def on_order_placed(
        self,
        order_id: str,
        instrument: str,
        side: str,
        quantity: Decimal,
        price: Optional[Decimal],
    ) -> None:
        """Notify the data source that an order has been placed.

        The data source uses this to:
        - live: register with broker's order stream (via FillMatcher)
        - paper: simulate immediate fill
        - backtest: no-op (fills come from historical data, not orders)
        - replay: no-op (fills come from replayed events)
        """
        ...

    @property
    @abstractmethod
    def is_live(self) -> bool:
        """True if this source involves a real broker connection."""
        ...


class BrokerDataSource(DataSource):
    """Live data source -- real broker connection, real-time fills via FillMatcher.

    In live mode, orders are sent to the broker. Fill updates arrive asynchronously
    from the broker's order stream (WebSocket or polling). The FillMatcher translates
    these updates into ApplyFillCommands that flow back through the on_fill callback.
    """

    def __init__(
        self,
        on_fill: FillCallback,
        broker_config: "BrokerConfig",
        command_processor: CommandProcessor,
    ) -> None:
        super().__init__(on_fill)
        self._broker_config = broker_config
        # Route matcher-emitted fills through the session's on_fill path so
        # command processing and projector updates stay atomic under the
        # session lock (prevents the live projector-bypass).
        self._fill_matcher = FillMatcher(
            command_processor, on_fill=self._apply_fill_command
        )
        self._started = False

    def _apply_fill_command(self, command: ApplyFillCommand) -> CommandResult:
        """Adapt the 4-arg FillCallback to the matcher's command callback.

        Preserves the matcher-generated fill_id (broker_id + cumulative
        quantity) so fill dedup stays stable across retries.
        """
        result = self._on_fill(
            command.order_id,
            command.cumulative_filled,
            command.fill_price,
            command.fill_id,
        )
        if result is None:
            # Callback produced no result — report failure so the matcher
            # does not advance its tracker (the next update can retry).
            return CommandResult(
                success=False,
                events=[],
                correlation_id=command.correlation_id,
                error="on_fill callback returned no result",
            )
        return result

    def start(self) -> None:
        """Connect to broker and start listening for order updates.

        NOTE: Real broker connection is not implemented here -- this session
        layer focuses on state management. The broker adapter layer (outside
        this module) is responsible for connecting and feeding updates
        into the FillMatcher via process_broker_update().
        """
        self._started = True
        log.info("BrokerDataSource started (broker=%s)", self._broker_config.broker_id)

    def stop(self) -> None:
        self._started = False
        log.info("BrokerDataSource stopped")

    def on_order_placed(
        self,
        order_id: str,
        instrument: str,
        side: str,
        quantity: Decimal,
        price: Optional[Decimal],
    ) -> None:
        """Register the order with the FillMatcher for live fill tracking.

        The broker assigns its own order ID when the order reaches the exchange.
        For now, we register with a placeholder broker_order_id -- the broker adapter
        updates this mapping via register_order() once the real ID is known.
        """
        if not self._started:
            log.warning("BrokerDataSource.on_order_placed called before start()")
            return

        # Use a deterministic broker_order_id derived from internal order_id.
        # The broker adapter should call register_order() with the real ID.
        broker_order_id = f"broker-{order_id[:8]}"
        self._fill_matcher.register_order(
            order_id=order_id,
            broker_order_id=broker_order_id,
            instrument=instrument,
            side=side,
        )

    def process_broker_update(self, update: BrokerOrderUpdate) -> None:
        """Process a broker order-stream update through the FillMatcher.

        Called by the broker adapter layer when a real order update arrives.
        The FillMatcher emits an ApplyFillCommand that flows back through the
        on_fill callback into the session, which processes the command and
        updates its projectors atomically.
        """
        result = self._fill_matcher.process_update(update)
        if result is not None and result.success:
            log.debug("Broker update processed: %s", update.broker_order_id)

    @property
    def is_live(self) -> bool:
        return True

    @property
    def fill_matcher(self) -> FillMatcher:
        return self._fill_matcher


class HistoricalDataSource(DataSource):
    """Backtest data source -- reads historical OHLCV data, simulates fills.

    In backtest mode, orders are filled against historical data. Fills are
    driven by the backtest engine feeding historical bars into the session,
    not by the orders themselves.
    """

    def __init__(self, on_fill: FillCallback, data_path: str) -> None:
        super().__init__(on_fill)
        self._data_path = data_path
        self._started = False

    def start(self) -> None:
        """Open the historical data source."""
        self._started = True
        log.info("HistoricalDataSource started (path=%s)", self._data_path)

    def stop(self) -> None:
        self._started = False
        log.info("HistoricalDataSource stopped")

    def on_order_placed(
        self,
        order_id: str,
        instrument: str,
        side: str,
        quantity: Decimal,
        price: Optional[Decimal],
    ) -> None:
        """No-op for backtest -- fills come from historical data, not orders."""
        pass

    @property
    def is_live(self) -> bool:
        return False

    @property
    def data_path(self) -> str:
        return self._data_path


class SimulatedDataSource(DataSource):
    """Paper trading data source -- simulated broker with instant fills.

    In paper mode, orders are filled immediately at the requested price
    (or current market price if available). This provides realistic
    fill simulation without real money.
    """

    def __init__(self, on_fill: FillCallback) -> None:
        super().__init__(on_fill)
        self._started = False

    def start(self) -> None:
        """Initialize the simulated broker."""
        self._started = True
        log.info("SimulatedDataSource (paper) started")

    def stop(self) -> None:
        self._started = False
        log.info("SimulatedDataSource (paper) stopped")

    def on_order_placed(
        self,
        order_id: str,
        instrument: str,
        side: str,
        quantity: Decimal,
        price: Optional[Decimal],
    ) -> None:
        """Simulate immediate fill at the order's limit price."""
        if not self._started:
            log.warning("SimulatedDataSource.on_order_placed called before start()")
            return

        if price is None or price <= 0:
            log.warning(
                "Paper fill skipped: order_id=%s has no valid price", order_id
            )
            return

        fill_id = f"paper-fill-{order_id[:8]}"
        log.info(
            "Paper fill: order_id=%s instrument=%s side=%s qty=%s price=%s",
            order_id, instrument, side, quantity, price,
        )
        self._on_fill(order_id, quantity, price, fill_id)

    @property
    def is_live(self) -> bool:
        return False


class ReplayDataSource(DataSource):
    """Replay data source -- replays events from a previous session.

    In replay mode, historical events are replayed at a configurable speed.
    Fills come from the replayed event stream, not from new broker connections.
    """

    def __init__(self, on_fill: FillCallback, data_path: str, replay_speed: float = 1.0) -> None:
        super().__init__(on_fill)
        self._data_path = data_path
        self._replay_speed = replay_speed
        self._started = False

    def start(self) -> None:
        """Open the replay data source."""
        self._started = True
        log.info(
            "ReplayDataSource started (path=%s, speed=%.1fx)",
            self._data_path, self._replay_speed,
        )

    def stop(self) -> None:
        self._started = False
        log.info("ReplayDataSource stopped")

    def on_order_placed(
        self,
        order_id: str,
        instrument: str,
        side: str,
        quantity: Decimal,
        price: Optional[Decimal],
    ) -> None:
        """No-op for replay -- fills come from replayed events."""
        pass

    @property
    def is_live(self) -> bool:
        return False

    @property
    def replay_speed(self) -> float:
        return self._replay_speed


def create_data_source(
    config: "DataSourceConfig",
    on_fill: FillCallback,
    command_processor: CommandProcessor,
) -> DataSource:
    """Factory: build the correct DataSource from DataSourceConfig.

    Args:
        config: Data source configuration with type and mode-specific fields.
        on_fill: Callback invoked when a fill needs to be applied to an order.
        command_processor: The session's command processor (used by live FillMatcher).

    Returns:
        A DataSource implementation matching the config.type.

    Raises:
        ValueError: If the data source type is invalid or required config is missing.
    """
    if config.type == "broker":
        if config.broker is None:
            raise ValueError("BrokerDataSource requires broker config")
        return BrokerDataSource(
            on_fill=on_fill,
            broker_config=config.broker,
            command_processor=command_processor,
        )
    elif config.type == "historical":
        if config.historical_path is None:
            raise ValueError("HistoricalDataSource requires historical_path")
        return HistoricalDataSource(
            on_fill=on_fill,
            data_path=config.historical_path,
        )
    elif config.type == "simulated":
        return SimulatedDataSource(on_fill=on_fill)
    elif config.type == "replay":
        if config.historical_path is None:
            raise ValueError("ReplayDataSource requires historical_path")
        return ReplayDataSource(
            on_fill=on_fill,
            data_path=config.historical_path,
            replay_speed=config.replay_speed or 1.0,
        )
    else:
        raise ValueError(f"Unknown data source type: {config.type!r}")
