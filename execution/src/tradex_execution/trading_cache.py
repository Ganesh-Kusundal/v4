"""In-memory cache for orders, positions, and latest quotes."""

from __future__ import annotations

import threading
from collections.abc import Sequence
from contextlib import ExitStack

from tradex_domain.execution import Order, Position
from tradex_domain.instruments import Instrument
from tradex_domain.market import Quote
from tradex_domain.protocols import TradingCacheProtocol
from tradex_domain.value_objects import InstrumentId, OrderId


class TradingCache(TradingCacheProtocol):
    """In-memory cache for orders, positions, and latest quotes.

    Each collection (orders, positions, quotes) is guarded by its own
    ``RLock`` used for **both** reads and writes (H1 fix — previously
    reads used a separate ``_sync_lock`` that did not exclude writers,
    creating a data race under free-threaded Python).
    """

    def __init__(self) -> None:
        self._orders: dict[str, Order] = {}
        self._positions: dict[str, Position] = {}
        self._quotes: dict[str, Quote] = {}
        self._orders_lock = threading.RLock()
        self._positions_lock = threading.RLock()
        self._quotes_lock = threading.RLock()
        #: Per-instrument locks for position read-modify-write. Owned here
        #: because this cache owns the book: every component that mutates a
        #: position (PositionAccountant, MarkToMarketService) must take the
        #: same lock, and a lock map held per-component silently fails to
        #: serialise anything (M-1).
        self._position_locks: dict[str, threading.RLock] = {}
        self._position_locks_guard = threading.Lock()

    # ------------------------------------------------------------------
    # Orders
    # ------------------------------------------------------------------

    def update_order(self, order: Order) -> None:
        """Insert or update an order keyed by order_id."""
        with self._orders_lock:
            self._orders[order.order_id.value] = order

    def set_order(self, order: Order) -> None:
        """Insert or update an order (alias for ``update_order``)."""
        with self._orders_lock:
            self._orders[order.order_id.value] = order

    def get_order(self, order_id: OrderId | str) -> Order | None:
        """Return the order with the given id, or None."""
        key = order_id.value if isinstance(order_id, OrderId) else order_id
        with self._orders_lock:
            return self._orders.get(key)

    def all_orders(self) -> list[Order]:
        """Return a snapshot list of all cached orders."""
        with self._orders_lock:
            return list(self._orders.values())

    # ------------------------------------------------------------------
    # Positions
    # ------------------------------------------------------------------

    @staticmethod
    def _instrument_key(instrument: Instrument | InstrumentId | str) -> str:
        """Resolve a flexible instrument reference to a string key."""
        if isinstance(instrument, Instrument):
            return str(instrument.instrument_id)
        if isinstance(instrument, InstrumentId):
            return str(instrument)
        return instrument

    def position_lock(self, instrument: Instrument | InstrumentId | str) -> threading.RLock:
        """Return the per-instrument lock guarding position read-modify-write.

        Hold this across a *read* of a position and the *write* derived from
        it, so a concurrent fill cannot be overwritten by a stale mark. Locks
        are per instrument, so unrelated symbols do not contend.
        """
        key = self._instrument_key(instrument)
        with self._position_locks_guard:
            lock = self._position_locks.get(key)
            if lock is None:
                lock = threading.RLock()
                self._position_locks[key] = lock
            return lock

    def update_position(self, position: Position) -> None:
        """Insert or update a position keyed by instrument id."""
        with self._positions_lock:
            self._positions[self._instrument_key(position.instrument)] = position

    def set_position(self, position: Position) -> None:
        """Insert or update a position keyed by instrument id."""
        with self._positions_lock:
            self._positions[self._instrument_key(position.instrument)] = position

    def _instrument_locks(self, positions: list[Position]) -> list[threading.RLock]:
        """One per-instrument lock per row, in row order.

        Called with rows already sorted by key so every batch applies in the
        same acquisition order (sorted-key order) regardless of caller order:
        two concurrent batches can never deadlock against each other, and the
        order is a refinement of the fill path's one-lock-at-a-time RMW, so no
        cycle with it exists either.
        """
        return [self.position_lock(position.instrument) for position in positions]

    def set_positions(self, positions: Sequence[Position]) -> None:
        """Write every row in *positions* as one batch, or none of them.

        The adoption apply's atomic seam (P0-F6). Why a batch method at all:
        the venue-position adoption writes a *plan* — a set of rows that are
        correct together. Written one ``set_position`` at a time, a fill's
        read-modify-write on any touched instrument can slip between two rows
        (the fill holds ``position_lock`` across its RMW; a per-row write takes
        only the dict lock and interleaves), and a reader sees a half-applied
        plan. Here every touched instrument lock is held — in sorted key order
        — across the whole swap, so neither can happen.

        Validation runs before any lock is taken or any row written: a bad row
        anywhere raises :class:`TypeError` with the book untouched, matching
        the plan's all-or-nothing semantics.
        """
        rows = list(positions)
        for position in rows:
            if not isinstance(position, Position):
                raise TypeError(
                    "set_positions requires Position rows, got "
                    f"{type(position).__name__}"
                )
        keyed = sorted(
            ((self._instrument_key(p.instrument), p) for p in rows),
            key=lambda item: item[0],
        )
        locks = self._instrument_locks([p for _, p in keyed])
        with ExitStack() as stack:
            for lock in locks:
                stack.enter_context(lock)
            with self._positions_lock:
                for key, position in keyed:
                    self._positions[key] = position

    def get_position(self, instrument: Instrument | InstrumentId | str) -> Position | None:
        """Return the position for the given instrument, or None."""
        key = self._instrument_key(instrument)
        with self._positions_lock:
            return self._positions.get(key)

    def all_positions(self) -> list[Position]:
        """Return a snapshot list of all cached positions."""
        with self._positions_lock:
            return list(self._positions.values())

    # ------------------------------------------------------------------
    # Quotes
    # ------------------------------------------------------------------

    def update_quote(self, quote: Quote) -> None:
        """Insert or update the latest quote keyed by instrument id."""
        with self._quotes_lock:
            self._quotes[self._instrument_key(quote.instrument)] = quote

    def set_quote(self, quote: Quote) -> None:
        """Insert or update the latest quote keyed by instrument id."""
        with self._quotes_lock:
            self._quotes[self._instrument_key(quote.instrument)] = quote

    def get_quote(self, instrument: Instrument | InstrumentId | str) -> Quote | None:
        """Return the latest quote for the given instrument, or None."""
        key = self._instrument_key(instrument)
        with self._quotes_lock:
            return self._quotes.get(key)

    # ------------------------------------------------------------------
    # Snapshots
    # ------------------------------------------------------------------

    def snapshot(self) -> dict[str, dict]:
        """Return a deep-copy dict of all cached state.

        The snapshot acquires all three collection locks in a fixed
        order (orders → positions → quotes) to prevent deadlocks and
        ensure a consistent cross-collection view.
        """
        with self._orders_lock:
            with self._positions_lock:
                with self._quotes_lock:
                    return {
                        "orders": dict(self._orders),
                        "positions": dict(self._positions),
                        "quotes": dict(self._quotes),
                    }

    def restore(self, snapshot: dict[str, dict]) -> None:
        """Replace internal state from a previous ``snapshot()``."""
        with self._orders_lock:
            with self._positions_lock:
                with self._quotes_lock:
                    self._orders = dict(snapshot.get("orders", {}))
                    self._positions = dict(snapshot.get("positions", {}))
                    self._quotes = dict(snapshot.get("quotes", {}))

    # ------------------------------------------------------------------
    # Lifecycle
    # ------------------------------------------------------------------

    def clear(self) -> None:
        """Drop all cached data."""
        with self._orders_lock:
            self._orders.clear()
        with self._positions_lock:
            self._positions.clear()
        with self._quotes_lock:
            self._quotes.clear()


