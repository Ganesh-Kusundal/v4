"""In-memory cache for orders, positions, and latest quotes."""

from __future__ import annotations

import threading

from tradex_domain.execution import Order, Position
from tradex_domain.instruments import Instrument
from tradex_domain.market import Quote
from tradex_domain.protocols import TradingCacheProtocol
from tradex_domain.value_objects import InstrumentId, OrderId


class _ReadWriteLock:
    """Simple read-write lock built on a Condition and counters.

    Multiple readers can hold the lock concurrently; a writer gets
    exclusive access.  Acquired as a context manager::

        with lock.reader():
            # read-only access

        with lock.writer():
            # read-write access

    # ponytail: Condition+counters, not stdlib RLock — readers don't block
    # readers. For OMS read-heavy / write-rare this wins; if contention never
    # shows, replace with RLock. Per-call inner classes are intentional (shortest diff).
    """

    def __init__(self) -> None:
        self._cond = threading.Condition(threading.Lock())
        self._readers = 0
        self._writer = False

    def reader(self):
        """Return a context manager for a read lock."""

        class _Reader:
            def __init__(self, lock: _ReadWriteLock) -> None:
                self._lock = lock

            def __enter__(self) -> None:
                with self._lock._cond:
                    while self._lock._writer:
                        self._lock._cond.wait()
                    self._lock._readers += 1

            def __exit__(self, *exc: object) -> None:
                with self._lock._cond:
                    self._lock._readers -= 1
                    if self._lock._readers == 0:
                        self._lock._cond.notify_all()

        return _Reader(self)

    def writer(self):
        """Return a context manager for a write lock."""

        class _Writer:
            def __init__(self, lock: _ReadWriteLock) -> None:
                self._lock = lock

            def __enter__(self) -> None:
                with self._lock._cond:
                    while self._lock._writer or self._lock._readers > 0:
                        self._lock._cond.wait()
                    self._lock._writer = True

            def __exit__(self, *exc: object) -> None:
                with self._lock._cond:
                    self._lock._writer = False
                    self._lock._cond.notify_all()

        return _Writer(self)


class TradingCache(TradingCacheProtocol):
    """In-memory cache for orders, positions, and latest quotes."""

    def __init__(self) -> None:
        self._orders: dict[str, Order] = {}
        self._positions: dict[str, Position] = {}
        self._quotes: dict[str, Quote] = {}
        self._lock = _ReadWriteLock()

    # ------------------------------------------------------------------
    # Orders
    # ------------------------------------------------------------------

    def update_order(self, order: Order) -> None:
        """Insert or update an order keyed by order_id."""
        with self._lock.writer():
            self._orders[order.order_id.value] = order

    def set_order(self, order: Order) -> None:
        """Insert or update an order (alias for ``update_order``)."""
        with self._lock.writer():
            self._orders[order.order_id.value] = order

    def get_order(self, order_id: OrderId | str) -> Order | None:
        """Return the order with the given id, or None."""
        key = order_id.value if isinstance(order_id, OrderId) else order_id
        with self._lock.reader():
            return self._orders.get(key)

    def all_orders(self) -> list[Order]:
        """Return a snapshot list of all cached orders."""
        with self._lock.reader():
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

    def update_position(self, position: Position) -> None:
        """Insert or update a position keyed by instrument id."""
        with self._lock.writer():
            self._positions[self._instrument_key(position.instrument)] = position

    def set_position(self, position: Position) -> None:
        """Insert or update a position keyed by instrument id."""
        with self._lock.writer():
            self._positions[self._instrument_key(position.instrument)] = position

    def get_position(self, instrument: Instrument | InstrumentId | str) -> Position | None:
        """Return the position for the given instrument, or None."""
        key = self._instrument_key(instrument)
        with self._lock.reader():
            return self._positions.get(key)

    def all_positions(self) -> list[Position]:
        """Return a snapshot list of all cached positions."""
        with self._lock.reader():
            return list(self._positions.values())

    # ------------------------------------------------------------------
    # Quotes
    # ------------------------------------------------------------------

    def update_quote(self, quote: Quote) -> None:
        """Insert or update the latest quote keyed by instrument id."""
        with self._lock.writer():
            self._quotes[self._instrument_key(quote.instrument)] = quote

    def set_quote(self, quote: Quote) -> None:
        """Insert or update the latest quote keyed by instrument id."""
        with self._lock.writer():
            self._quotes[self._instrument_key(quote.instrument)] = quote

    def get_quote(self, instrument: Instrument | InstrumentId | str) -> Quote | None:
        """Return the latest quote for the given instrument, or None."""
        key = self._instrument_key(instrument)
        with self._lock.reader():
            return self._quotes.get(key)

    # ------------------------------------------------------------------
    # Snapshots
    # ------------------------------------------------------------------

    def snapshot(self) -> dict[str, dict]:
        """Return a deep-copy dict of all cached state.

        The snapshot is taken under a single read lock so that all three
        collections are consistent with one another.  The returned dicts
        contain shallow copies of the stored objects — callers should not
        mutate the cached Order/Position/Quote instances through the
        snapshot (the cache itself is single-process; mutating returned
        references would bypass the lock contract).
        """
        with self._lock.reader():
            return {
                "orders": dict(self._orders),
                "positions": dict(self._positions),
                "quotes": dict(self._quotes),
            }

    def restore(self, snapshot: dict[str, dict]) -> None:
        """Replace internal state from a previous ``snapshot()``."""
        with self._lock.writer():
            self._orders = dict(snapshot.get("orders", {}))
            self._positions = dict(snapshot.get("positions", {}))
            self._quotes = dict(snapshot.get("quotes", {}))

    # ------------------------------------------------------------------
    # Lifecycle
    # ------------------------------------------------------------------

    def clear(self) -> None:
        """Drop all cached data."""
        with self._lock.writer():
            self._orders.clear()
            self._positions.clear()
            self._quotes.clear()


__all__ = ["TradingCache"]
