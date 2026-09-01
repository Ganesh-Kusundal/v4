"""Session Recovery — rebuilds state from event log and reconciles with broker.

On startup, the session recovers by replaying events from the EventStore.
After recovery, it reconciles local state with broker state to detect drift.

Architecture:
    EventStore → OrderBookActor.recover() → CommandProcessor.recover() → reconcile_with_broker()

The recovery process is deterministic: same events → same state, always.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Optional

from tradex_trading.events.actor import OrderBookActor
from tradex_trading.events.fill_matcher import BrokerOrderUpdate
from tradex_trading.events.processor import CommandProcessor
from tradex_trading.events.store import EventStore


@dataclass
class RecoveryResult:
    """Result of session recovery from event log.

    Attributes:
        success: True if recovery completed without errors.
        orders_recovered: Number of orders recovered from the event log.
        positions_recovered: Number of positions recovered from the event log.
        last_sequence: The last sequence number processed.
        error: Error message if recovery failed, None otherwise.
    """

    success: bool
    orders_recovered: int
    positions_recovered: int
    last_sequence: int
    error: Optional[str] = None


@dataclass(frozen=True, slots=True)
class Discrepancy:
    """A discrepancy between local and broker state for a single order.

    Attributes:
        order_id: The internal order ID with the discrepancy.
        field: Which field differs (e.g., "filled_quantity", "status").
        local_value: The local value for the field.
        broker_value: The broker's value for the field.
    """

    order_id: str
    field: str
    local_value: str
    broker_value: str


@dataclass
class ReconciliationResult:
    """Result of reconciling local state with broker state.

    Attributes:
        in_sync: True if local and broker state match perfectly.
        discrepancies: List of detected discrepancies.
        local_only_orders: Orders present locally but missing at broker.
        broker_only_orders: Orders present at broker but missing locally.
    """

    in_sync: bool
    discrepancies: list[Discrepancy] = field(default_factory=list)
    local_only_orders: list[str] = field(default_factory=list)
    broker_only_orders: list[str] = field(default_factory=list)


class SessionRecovery:
    """Rebuilds session state from event log and reconciles with broker.

    Usage:
        recovery = SessionRecovery(event_store, order_book, command_processor)
        result = recovery.recover()
        if result.success:
            recon = recovery.reconcile_with_broker(broker_orders)
    """

    def __init__(
        self,
        event_store: EventStore,
        order_book: OrderBookActor,
        command_processor: CommandProcessor,
    ) -> None:
        self._store = event_store
        self._actor = order_book
        self._processor = command_processor

    def recover(self) -> RecoveryResult:
        """Rebuild state from event log by replaying all events.

        Rebuilds:
        - Order book state (orders, positions, kill switch)
        - Idempotency map (to detect duplicate commands after recovery)

        Returns:
            RecoveryResult with counts and success status.
        """
        try:
            # Recover order book state from event log
            self._actor.recover()

            # Recover idempotency map from event log
            self._processor.recover()

            # Get counts from recovered state
            snapshot = self._actor.snapshot()
            orders_recovered = len(snapshot["orders"])
            positions_recovered = len(snapshot["positions"])

            # Get last sequence number
            last_sequence = self._store.get_last_sequence(self._actor._session_id)

            return RecoveryResult(
                success=True,
                orders_recovered=orders_recovered,
                positions_recovered=positions_recovered,
                last_sequence=last_sequence,
            )
        except Exception as e:
            return RecoveryResult(
                success=False,
                orders_recovered=0,
                positions_recovered=0,
                last_sequence=0,
                error=str(e),
            )

    def reconcile_with_broker(
        self, broker_orders: list[BrokerOrderUpdate]
    ) -> ReconciliationResult:
        """Compare local state with broker state to detect drift.

        Detects:
        - Fills that broker has but local doesn't (filled_quantity mismatch)
        - Orders that local has but broker doesn't (local-only orders)
        - Orders that broker has but local doesn't (broker-only orders)

        Args:
            broker_orders: List of current broker order updates.

        Returns:
            ReconciliationResult with discrepancies and sync status.
        """
        discrepancies: list[Discrepancy] = []
        local_only_orders: list[str] = []
        broker_only_orders: list[str] = []

        # Build lookup maps
        # We need to correlate local orders with broker orders.
        # The broker orders have broker_order_id; local orders have order_id.
        # For reconciliation, we compare by matching instruments and sides
        # since the FillMatcher's mapping may not exist during early recovery.
        #
        # Strategy: match by (instrument, side) pair. If broker has an update
        # for which no local order exists with same instrument+side, it's broker-only.
        # If local has an order with no matching broker update, it's local-only.

        snapshot = self._actor.snapshot()
        local_orders = snapshot["orders"]

        # Build map of (instrument, side) -> local order
        local_by_key: dict[tuple[str, str], dict] = {}
        for oid, order in local_orders.items():
            key = (order["instrument"], order["side"])
            local_by_key[key] = order

        # Build map of (instrument, side) -> broker order
        broker_by_key: dict[tuple[str, str], BrokerOrderUpdate] = {}
        for bupd in broker_orders:
            key = (bupd.instrument, bupd.side)
            broker_by_key[key] = bupd

        # Check for discrepancies in matched orders
        for key, local_order in local_by_key.items():
            if key in broker_by_key:
                broker_update = broker_by_key[key]
                local_filled = local_order["filled_quantity"]
                broker_filled = str(broker_update.filled_quantity)

                if local_filled != broker_filled:
                    discrepancies.append(
                        Discrepancy(
                            order_id=local_order["order_id"],
                            field="filled_quantity",
                            local_value=local_filled,
                            broker_value=broker_filled,
                        )
                    )
            else:
                # Local order has no matching broker order
                local_only_orders.append(local_order["order_id"])

        # Check for broker-only orders
        for key, broker_update in broker_by_key.items():
            if key not in local_by_key:
                broker_only_orders.append(broker_update.broker_order_id)

        in_sync = (
            len(discrepancies) == 0
            and len(local_only_orders) == 0
            and len(broker_only_orders) == 0
        )

        return ReconciliationResult(
            in_sync=in_sync,
            discrepancies=discrepancies,
            local_only_orders=local_only_orders,
            broker_only_orders=broker_only_orders,
        )
