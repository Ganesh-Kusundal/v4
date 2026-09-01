"""Session Recovery — rebuilds state from event log and reconciles with broker.

On startup, the session recovers by replaying events from the EventStore.
After recovery, it reconciles local state with broker state to detect drift.

Architecture:
    EventStore → OrderBookActor.recover() → CommandProcessor.recover() → reconcile_with_broker()

The recovery process is deterministic: same events → same state, always.
"""

from __future__ import annotations

import sqlite3
from dataclasses import dataclass, field
from decimal import Decimal
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
            recon = recovery.reconcile_with_broker(broker_orders, broker_to_internal)
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
            # Clear state before replay to ensure idempotency.
            # Without this, calling recover() on an actor with existing state
            # would duplicate entries.
            self._actor.reset_state()

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
        except (sqlite3.Error, ValueError, KeyError, TypeError) as e:
            return RecoveryResult(
                success=False,
                orders_recovered=0,
                positions_recovered=0,
                last_sequence=0,
                error=str(e),
            )

    def reconcile_with_broker(
        self,
        broker_orders: list[BrokerOrderUpdate],
        broker_to_internal: Optional[dict[str, str]] = None,
    ) -> ReconciliationResult:
        """Compare local state with broker state to detect drift.

        Detects:
        - Fills that broker has but local doesn't (filled_quantity mismatch)
        - Orders that local has but broker doesn't (local-only orders)
        - Orders that broker has but local doesn't (broker-only orders)

        Args:
            broker_orders: List of current broker order updates.
            broker_to_internal: Optional mapping from broker_order_id to internal order_id.
                If not provided, falls back to matching by (instrument, side).

        Returns:
            ReconciliationResult with discrepancies and sync status.
        """
        discrepancies: list[Discrepancy] = []
        local_only_orders: list[str] = []
        broker_only_orders: list[str] = []

        snapshot = self._actor.snapshot()
        local_orders = snapshot["orders"]

        if broker_to_internal is not None:
            # Use explicit mapping from broker_order_id to order_id.
            # This is the correct approach when the FillMatcher's mapping is available.
            self._reconcile_with_mapping(
                local_orders, broker_orders, broker_to_internal,
                discrepancies, local_only_orders, broker_only_orders,
            )
        else:
            # Fallback: match by (instrument, side).
            # WARNING: This can only detect broker-only/local-only orders correctly
            # when there's at most one order per (instrument, side). For multiple
            # orders on the same instrument+side, use broker_to_internal mapping.
            self._reconcile_by_instrument_side(
                local_orders, broker_orders,
                discrepancies, local_only_orders, broker_only_orders,
            )

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

    def _reconcile_with_mapping(
        self,
        local_orders: dict[str, dict],
        broker_orders: list[BrokerOrderUpdate],
        broker_to_internal: dict[str, str],
        discrepancies: list[Discrepancy],
        local_only_orders: list[str],
        broker_only_orders: list[str],
    ) -> None:
        """Reconcile using explicit broker_order_id → order_id mapping."""
        # Build set of broker_order_ids that have updates
        broker_ids_with_updates = {bupd.broker_order_id for bupd in broker_orders}

        # Build map of broker_order_id -> broker update
        broker_by_id: dict[str, BrokerOrderUpdate] = {
            bupd.broker_order_id: bupd for bupd in broker_orders
        }

        # Check each local order
        for order_id, local_order in local_orders.items():
            # Find broker_order_id for this internal order_id
            broker_id = None
            for bid, oid in broker_to_internal.items():
                if oid == order_id:
                    broker_id = bid
                    break

            if broker_id is None:
                # No mapping found — local order has no broker counterpart
                local_only_orders.append(order_id)
                continue

            if broker_id in broker_by_id:
                broker_update = broker_by_id[broker_id]
                local_filled = Decimal(local_order["filled_quantity"])
                broker_filled = Decimal(str(broker_update.filled_quantity))

                if local_filled != broker_filled:
                    discrepancies.append(
                        Discrepancy(
                            order_id=order_id,
                            field="filled_quantity",
                            local_value=str(local_filled),
                            broker_value=str(broker_filled),
                        )
                    )
            else:
                # Local order has mapping but no broker update
                local_only_orders.append(order_id)

        # Check for broker-only orders
        for bupd in broker_orders:
            if bupd.broker_order_id in broker_to_internal:
                internal_id = broker_to_internal[bupd.broker_order_id]
                if internal_id not in local_orders:
                    broker_only_orders.append(bupd.broker_order_id)
            else:
                # Broker order has no mapping to internal order
                broker_only_orders.append(bupd.broker_order_id)

    def _reconcile_by_instrument_side(
        self,
        local_orders: dict[str, dict],
        broker_orders: list[BrokerOrderUpdate],
        discrepancies: list[Discrepancy],
        local_only_orders: list[str],
        broker_only_orders: list[str],
    ) -> None:
        """Reconcile by matching (instrument, side) — handles multiple orders correctly."""
        # Build map of (instrument, side) -> list of local orders
        local_by_key: dict[tuple[str, str], list[dict]] = {}
        for oid, order in local_orders.items():
            key = (order["instrument"], order["side"])
            if key not in local_by_key:
                local_by_key[key] = []
            local_by_key[key].append(order)

        # Build map of (instrument, side) -> list of broker updates
        broker_by_key: dict[tuple[str, str], list[BrokerOrderUpdate]] = {}
        for bupd in broker_orders:
            key = (bupd.instrument, bupd.side)
            if key not in broker_by_key:
                broker_by_key[key] = []
            broker_by_key[key].append(bupd)

        # Check for discrepancies in matched orders
        for key, local_list in local_by_key.items():
            if key in broker_by_key:
                broker_list = broker_by_key[key]
                # Match local orders with broker updates by index
                # (best-effort when no explicit mapping available)
                for i, local_order in enumerate(local_list):
                    if i < len(broker_list):
                        broker_update = broker_list[i]
                        local_filled = Decimal(local_order["filled_quantity"])
                        broker_filled = Decimal(str(broker_update.filled_quantity))

                        if local_filled != broker_filled:
                            discrepancies.append(
                                Discrepancy(
                                    order_id=local_order["order_id"],
                                    field="filled_quantity",
                                    local_value=str(local_filled),
                                    broker_value=str(broker_filled),
                                )
                            )
                    else:
                        # More local orders than broker updates
                        local_only_orders.append(local_order["order_id"])
            else:
                # No broker updates for this (instrument, side)
                for local_order in local_list:
                    local_only_orders.append(local_order["order_id"])

        # Check for broker-only orders
        for key, broker_list in broker_by_key.items():
            if key in local_by_key:
                local_list = local_by_key[key]
                # Extra broker updates beyond local orders
                if len(broker_list) > len(local_list):
                    for i in range(len(local_list), len(broker_list)):
                        broker_only_orders.append(broker_list[i].broker_order_id)
            else:
                # No local orders for this (instrument, side)
                for bupd in broker_list:
                    broker_only_orders.append(bupd.broker_order_id)
