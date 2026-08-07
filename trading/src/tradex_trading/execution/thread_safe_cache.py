"""Thread-safe wrapper around TradingCache using threading.RLock."""

from __future__ import annotations

import threading

from tradex_domain.execution import Order, Position
from tradex_domain.instruments import Instrument
from tradex_domain.market import Quote
from tradex_domain.protocols import TradingCacheProtocol
from tradex_domain.value_objects import InstrumentId, OrderId

from tradex_trading.execution.trading_cache import TradingCache


class ThreadSafeTradingCache(TradingCacheProtocol):
    """Thread-safe facade over :class:`TradingCache`.

    Every public method acquires an ``RLock`` before delegating to the
    inner cache, ensuring atomic reads/writes from concurrent threads.
    """

    def __init__(self, cache: TradingCache | None = None) -> None:
        self._lock = threading.RLock()
        self._cache = cache if cache is not None else TradingCache()

    # ------------------------------------------------------------------
    # Orders
    # ------------------------------------------------------------------

    def update_order(self, order: Order) -> None:
        with self._lock:
            self._cache.update_order(order)

    def set_order(self, order: Order) -> None:
        with self._lock:
            self._cache.set_order(order)

    def get_order(self, order_id: OrderId | str) -> Order | None:
        with self._lock:
            return self._cache.get_order(order_id)

    def all_orders(self) -> list[Order]:
        with self._lock:
            return self._cache.all_orders()

    # ------------------------------------------------------------------
    # Positions
    # ------------------------------------------------------------------

    def update_position(self, position: Position) -> None:
        with self._lock:
            self._cache.update_position(position)

    def set_position(self, position: Position) -> None:
        with self._lock:
            self._cache.set_position(position)

    def get_position(self, instrument: Instrument | InstrumentId | str) -> Position | None:
        with self._lock:
            return self._cache.get_position(instrument)

    def all_positions(self) -> list[Position]:
        with self._lock:
            return self._cache.all_positions()

    # ------------------------------------------------------------------
    # Quotes
    # ------------------------------------------------------------------

    def update_quote(self, quote: Quote) -> None:
        with self._lock:
            self._cache.update_quote(quote)

    def set_quote(self, quote: Quote) -> None:
        with self._lock:
            self._cache.set_quote(quote)

    def get_quote(self, instrument: Instrument | InstrumentId | str) -> Quote | None:
        with self._lock:
            return self._cache.get_quote(instrument)

    # ------------------------------------------------------------------
    # Snapshots
    # ------------------------------------------------------------------

    def snapshot(self) -> dict[str, dict]:
        """Return a deep-copy snapshot under the lock for atomicity."""
        with self._lock:
            return self._cache.snapshot()

    def restore(self, snapshot: dict[str, dict]) -> None:
        """Replace internal state from a snapshot under the lock."""
        with self._lock:
            self._cache.restore(snapshot)

    # ------------------------------------------------------------------
    # Lifecycle
    # ------------------------------------------------------------------

    def clear(self) -> None:
        with self._lock:
            self._cache.clear()


__all__ = ["ThreadSafeTradingCache"]
