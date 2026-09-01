"""Fill Matcher — translates broker order-stream updates into ApplyFillCommands.

Replaces the legacy LiveFillBridge with a cleaner, testable component.

Architecture:
    Broker Stream → BrokerOrderUpdate → FillMatcher → ApplyFillCommand → CommandProcessor

The FillMatcher:
    - Maps internal order IDs to broker order IDs
    - Tracks last processed (broker_order_id, filled_quantity) for idempotency
    - Emits ApplyFillCommands for new fills only
    - Skips duplicate and stale updates silently
"""

from __future__ import annotations

import logging
import uuid
from dataclasses import dataclass
from datetime import datetime
from decimal import Decimal
from typing import Optional

from tradex_trading.events.actor import ApplyFillCommand
from tradex_trading.events.processor import CommandProcessor, CommandResult

log = logging.getLogger(__name__)


@dataclass
class BrokerOrderUpdate:
    """A broker order-stream update.

    Represents a single update from the broker's order stream (WebSocket or polling).
    The broker reports cumulative filled quantity — the FillMatcher computes deltas.
    """

    broker_order_id: str
    instrument: str
    side: str
    quantity: int
    filled_quantity: int
    fill_price: float
    status: str
    timestamp: datetime


@dataclass(frozen=True, slots=True)
class _OrderMapping:
    """Internal mapping from broker order to internal order."""

    order_id: str
    broker_order_id: str
    instrument: str
    side: str


class FillMatcher:
    """Translates broker order-stream updates into ApplyFillCommands.

    Replaces LiveFillBridge with a cleaner, testable component.

    Idempotency:
        Tracks last processed filled_quantity per broker_order_id.
        - Duplicate updates (same filled_quantity) → skipped, returns None
        - Stale updates (lower filled_quantity) → skipped, returns None
        - New updates (higher filled_quantity) → ApplyFillCommand emitted

    Thread safety:
        This class is NOT thread-safe. It is designed to be called from a
        single-threaded broker stream handler (same as the old LiveFillBridge).
    """

    def __init__(self, command_processor: CommandProcessor):
        self._processor = command_processor
        # broker_order_id → _OrderMapping
        self._broker_to_internal: dict[str, _OrderMapping] = {}
        # broker_order_id → last processed filled_quantity
        self._last_filled: dict[str, int] = {}

    def register_order(
        self,
        order_id: str,
        broker_order_id: str,
        instrument: str,
        side: str,
    ) -> None:
        """Map an internal order to a broker order.

        Call this when an order is placed and the broker assigns an ID.
        """
        mapping = _OrderMapping(
            order_id=order_id,
            broker_order_id=broker_order_id,
            instrument=instrument,
            side=side,
        )
        self._broker_to_internal[broker_order_id] = mapping
        self._last_filled[broker_order_id] = 0

    def process_update(self, update: BrokerOrderUpdate) -> Optional[CommandResult]:
        """Process a broker order update.

        If the update matches a registered order and has new fill quantity,
        creates an ApplyFillCommand and sends it to the CommandProcessor.

        Returns:
            CommandResult if a fill was applied, None if skipped or unknown.
        """
        broker_id = update.broker_order_id

        # Match broker update to internal order
        mapping = self._broker_to_internal.get(broker_id)
        if mapping is None:
            log.warning(
                "Received update for unknown broker_order_id=%s (status=%s)",
                broker_id,
                update.status,
            )
            return None

        # Idempotency check — skip duplicate or stale updates
        last_filled = self._last_filled.get(broker_id, 0)
        if update.filled_quantity <= last_filled:
            # Duplicate (equal) or stale (lower) — skip
            return None

        # Create ApplyFillCommand
        fill_id = f"fill-{broker_id}-{update.filled_quantity}"
        command = ApplyFillCommand(
            order_id=mapping.order_id,
            cumulative_filled=Decimal(str(update.filled_quantity)),
            fill_price=Decimal(str(update.fill_price)),
            fill_id=fill_id,
            correlation_id=str(uuid.uuid4()),
            event_time=update.timestamp,
        )

        # Send to processor
        result = self._processor.process(command)

        if result.success and result.events:
            # ONLY advance tracker AFTER successful processing with events.
            # If the actor returns empty events (rejection/no-op), do NOT
            # advance — this allows the next update to retry the fill.
            self._last_filled[broker_id] = update.filled_quantity
            log.info(
                "Fill applied: order_id=%s broker_id=%s filled=%d price=%s",
                mapping.order_id,
                broker_id,
                update.filled_quantity,
                update.fill_price,
            )
        elif not result.success:
            log.warning(
                "Fill rejected: order_id=%s broker_id=%s reason=%s",
                mapping.order_id,
                broker_id,
                result.error,
            )
        else:
            log.info(
                "Fill no-op (empty events): order_id=%s broker_id=%s filled=%d — tracker not advanced",
                mapping.order_id,
                broker_id,
                update.filled_quantity,
            )

        return result

    def unregister_order(self, order_id: str) -> None:
        """Remove mapping for an internal order ID.

        Finds the mapping by internal order_id and removes it.
        """
        # Find broker_order_id for this internal order_id
        broker_id = None
        for bid, mapping in self._broker_to_internal.items():
            if mapping.order_id == order_id:
                broker_id = bid
                break

        if broker_id is not None:
            del self._broker_to_internal[broker_id]
            self._last_filled.pop(broker_id, None)
