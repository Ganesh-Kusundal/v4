"""In-memory cache for orders, positions, and latest quotes."""

from __future__ import annotations

import threading

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

    def update_position(self, position: Position) -> None:
        """Insert or update a position keyed by instrument id."""
        with self._positions_lock:
            self._positions[self._instrument_key(position.instrument)] = position

    def set_position(self, position: Position) -> None:
        """Insert or update a position keyed by instrument id."""
        with self._positions_lock:
            self._positions[self._instrument_key(position.instrument)] = position

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


__all__ = ["TradingCache"]
