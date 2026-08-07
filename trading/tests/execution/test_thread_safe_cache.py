"""Tests for ThreadSafeTradingCache."""

from __future__ import annotations

import threading
from decimal import Decimal

from tradex_domain import (
    Equity,
    Money,
    Order,
    OrderId,
    OrderSide,
    OrderStatus,
    OrderType,
    Position,
    Price,
    Quantity,
    Quote,
    TimeInForce,
)

from tradex_trading.execution.thread_safe_cache import ThreadSafeTradingCache
from tradex_trading.execution.trading_cache import TradingCache

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _eq() -> Equity:
    return Equity.of("NSE", "RELIANCE")


def _make_order(oid: str) -> Order:
    return Order(
        order_id=OrderId(value=oid),
        instrument=_eq(),
        side=OrderSide.BUY,
        order_type=OrderType.LIMIT,
        quantity=Quantity(value=Decimal("10")),
        price=Price(value=Decimal("100")),
        time_in_force=TimeInForce.DAY,
        status=OrderStatus.NEW,
    )


def _make_position() -> Position:
    return Position(
        instrument=_eq(),
        quantity=Quantity(value=Decimal("10")),
        avg_price=Price(value=Decimal("100")),
        realized_pnl=Money(amount=Decimal("0"), currency="INR"),
        unrealized_pnl=Money(amount=Decimal("0"), currency="INR"),
    )


def _make_quote() -> Quote:
    return Quote(
        instrument=_eq(),
        ltp=Price(value=Decimal("150")),
    )


# ---------------------------------------------------------------------------
# Concurrent order tests
# ---------------------------------------------------------------------------

class TestConcurrentOrders:
    def test_concurrent_update_and_get(self) -> None:
        cache = ThreadSafeTradingCache()
        n_threads = 20
        n_orders = 50
        errors: list[Exception] = []

        def writer(start: int) -> None:
            try:
                for i in range(n_orders):
                    cache.update_order(_make_order(f"O-{start}-{i}"))
            except Exception as exc:  # pragma: no cover
                errors.append(exc)

        def reader() -> None:
            try:
                for _ in range(n_orders):
                    cache.get_order("O-0-0")
                    cache.all_orders()
            except Exception as exc:  # pragma: no cover
                errors.append(exc)

        threads: list[threading.Thread] = []
        for t in range(n_threads):
            if t % 2 == 0:
                threads.append(threading.Thread(target=writer, args=(t,)))
            else:
                threads.append(threading.Thread(target=reader))

        for th in threads:
            th.start()
        for th in threads:
            th.join()

        assert errors == []
        all_orders = cache.all_orders()
        assert len(all_orders) >= n_orders  # writer 0 wrote 50 unique orders


# ---------------------------------------------------------------------------
# Snapshot / restore under concurrency
# ---------------------------------------------------------------------------

class TestConcurrentSnapshotRestore:
    def test_snapshot_restore_no_corruption(self) -> None:
        cache = ThreadSafeTradingCache()
        for i in range(10):
            cache.update_order(_make_order(f"init-{i}"))

        errors: list[Exception] = []
        snap_results: list[dict] = []

        def snapper() -> None:
            try:
                for _ in range(100):
                    snap = cache.snapshot()
                    snap_results.append(snap)
            except Exception as exc:  # pragma: no cover
                errors.append(exc)

        def restorer() -> None:
            try:
                for _ in range(100):
                    snap = cache.snapshot()
                    cache.restore(snap)
            except Exception as exc:  # pragma: no cover
                errors.append(exc)

        def writer() -> None:
            try:
                for i in range(100):
                    cache.update_order(_make_order(f"concurrent-{i}"))
            except Exception as exc:  # pragma: no cover
                errors.append(exc)

        threads = [
            threading.Thread(target=snapper),
            threading.Thread(target=restorer),
            threading.Thread(target=writer),
        ]
        for th in threads:
            th.start()
        for th in threads:
            th.join()

        assert errors == []
        for snap in snap_results:
            assert "orders" in snap
            assert isinstance(snap["orders"], dict)


# ---------------------------------------------------------------------------
# Interface compatibility
# ---------------------------------------------------------------------------

class TestInterfaceCompatibility:
    def test_has_same_public_methods_as_trading_cache(self) -> None:
        expected_methods = {
            "update_order", "set_order", "get_order", "all_orders",
            "update_position", "set_position", "get_position", "all_positions",
            "update_quote", "set_quote", "get_quote",
            "snapshot", "restore", "clear",
        }
        ts_cache = ThreadSafeTradingCache()
        for method_name in expected_methods:
            assert hasattr(ts_cache, method_name), f"Missing method: {method_name}"
            assert callable(getattr(ts_cache, method_name))

    def test_delegates_correctly(self) -> None:
        cache = ThreadSafeTradingCache()

        order = _make_order("delegate-1")
        cache.update_order(order)
        assert cache.get_order("delegate-1") is order

        pos = _make_position()
        cache.update_position(pos)
        assert cache.get_position(_eq().symbol) is pos

        quote = _make_quote()
        cache.update_quote(quote)
        assert cache.get_quote(_eq().symbol) is quote

        cache.clear()
        assert cache.all_orders() == []
        assert cache.all_positions() == []

    def test_accepts_injected_cache(self) -> None:
        inner = TradingCache()
        inner.update_order(_make_order("pre-existing"))
        wrapper = ThreadSafeTradingCache(cache=inner)
        assert wrapper.get_order("pre-existing") is not None
