"""In-memory cache for orders, positions, and latest quotes."""

from __future__ import annotations

from tradex_domain.execution import Order, Position
from tradex_domain.instruments import Instrument
from tradex_domain.market import Quote
from tradex_domain.protocols import TradingCacheProtocol
from tradex_domain.value_objects import InstrumentId, OrderId


class TradingCache(TradingCacheProtocol):
    """In-memory cache for orders, positions, and latest quotes."""

    def __init__(self) -> None:
        self._orders: dict[str, Order] = {}
        self._positions: dict[str, Position] = {}
        self._quotes: dict[str, Quote] = {}

    # ------------------------------------------------------------------
    # Orders
    # ------------------------------------------------------------------

    def update_order(self, order: Order) -> None:
        """Insert or update an order keyed by order_id."""
        self._orders[order.order_id.value] = order

    def set_order(self, order: Order) -> None:
        """Insert or update an order (alias for ``update_order``)."""
        self._orders[order.order_id.value] = order

    def get_order(self, order_id: OrderId | str) -> Order | None:
        """Return the order with the given id, or None."""
        key = order_id.value if isinstance(order_id, OrderId) else order_id
        return self._orders.get(key)

    def all_orders(self) -> list[Order]:
        """Return a snapshot list of all cached orders."""
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
        """Insert or update a position keyed by instrument symbol."""
        self._positions[position.instrument.symbol] = position

    def set_position(self, position: Position) -> None:
        """Insert or update a position keyed by instrument id."""
        self._positions[self._instrument_key(position.instrument)] = position

    def get_position(self, instrument: Instrument | InstrumentId | str) -> Position | None:
        """Return the position for the given instrument, or None."""
        return self._positions.get(self._instrument_key(instrument))

    def all_positions(self) -> list[Position]:
        """Return a snapshot list of all cached positions."""
        return list(self._positions.values())

    # ------------------------------------------------------------------
    # Quotes
    # ------------------------------------------------------------------

    def update_quote(self, quote: Quote) -> None:
        """Insert or update the latest quote keyed by instrument symbol."""
        self._quotes[quote.instrument.symbol] = quote

    def set_quote(self, quote: Quote) -> None:
        """Insert or update the latest quote keyed by instrument id."""
        self._quotes[self._instrument_key(quote.instrument)] = quote

    def get_quote(self, instrument: Instrument | InstrumentId | str) -> Quote | None:
        """Return the latest quote for the given instrument, or None."""
        return self._quotes.get(self._instrument_key(instrument))

    # ------------------------------------------------------------------
    # Snapshots
    # ------------------------------------------------------------------

    def snapshot(self) -> dict[str, dict]:
        """Return a deep-copy dict of all cached state."""
        return {
            "orders": dict(self._orders),
            "positions": dict(self._positions),
            "quotes": dict(self._quotes),
        }

    def restore(self, snapshot: dict[str, dict]) -> None:
        """Replace internal state from a previous ``snapshot()``."""
        self._orders = dict(snapshot.get("orders", {}))
        self._positions = dict(snapshot.get("positions", {}))
        self._quotes = dict(snapshot.get("quotes", {}))

    # ------------------------------------------------------------------
    # Lifecycle
    # ------------------------------------------------------------------

    def clear(self) -> None:
        """Drop all cached data."""
        self._orders.clear()
        self._positions.clear()
        self._quotes.clear()


__all__ = ["TradingCache"]
