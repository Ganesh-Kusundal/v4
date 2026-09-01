"""Command Processor — entry point for all commands with idempotency."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Optional

from tradex_trading.events.actor import OrderBookActor
from tradex_trading.events.store import Event, EventStore


@dataclass
class CommandResult:
    """Result of processing a command."""

    success: bool
    events: list[Event]
    correlation_id: str
    error: Optional[str] = None
    is_duplicate: bool = False


class CommandProcessor:
    """Entry point for all commands.

    Checks idempotency, sends commands to the OrderBookActor, and persists
    events. If a command was already processed (same correlation_id), it
    returns the cached result.

    Idempotency is critical — no duplicate orders.
    """

    def __init__(self, event_store: EventStore, order_book: OrderBookActor):
        self._store = event_store
        self._actor = order_book
        self._idempotency: dict[str, CommandResult] = {}

    def process(self, command) -> CommandResult:
        """Process a command, return result.

        If the command was already processed (same correlation_id),
        return the cached result without re-executing.
        """
        correlation_id = command.correlation_id

        # Idempotency check — return cached result if already processed
        if correlation_id in self._idempotency:
            cached = self._idempotency[correlation_id]
            return CommandResult(
                success=cached.success,
                events=cached.events,
                correlation_id=cached.correlation_id,
                error=cached.error,
                is_duplicate=True,
            )

        # Delegate to actor for processing
        events = self._actor.handle(command)

        # Determine success/failure from events
        if events and events[0].type == "OrderRejected":
            result = CommandResult(
                success=False,
                events=events,
                correlation_id=correlation_id,
                error=events[0].payload.get("reason", "rejected"),
                is_duplicate=False,
            )
            # Do NOT cache rejections — release idempotency key for retry
            return result

        # Empty events (e.g., cancel unknown order) — treat as no-op, not cached
        if not events:
            return CommandResult(
                success=True,
                events=[],
                correlation_id=correlation_id,
                is_duplicate=False,
            )

        # Success — cache the result
        result = CommandResult(
            success=True,
            events=events,
            correlation_id=correlation_id,
            is_duplicate=False,
        )
        self._idempotency[correlation_id] = result
        return result

    def recover(self) -> None:
        """Rebuild idempotency map from event log.

        On startup, scan the event log for all correlation_ids and cache
        the results so duplicates are detected after recovery.
        """
        events = self._store.read_all(self._actor._session_id)

        # Group events by correlation_id, preserving order
        grouped: dict[str, list[Event]] = {}
        for event in events:
            cid = event.correlation_id
            if cid not in grouped:
                grouped[cid] = []
            grouped[cid].append(event)

        # Rebuild idempotency map from successful commands only
        for cid, evts in grouped.items():
            if evts[0].type != "OrderRejected":
                self._idempotency[cid] = CommandResult(
                    success=True,
                    events=evts,
                    correlation_id=cid,
                    is_duplicate=False,
                )
