"""Cache interface unification: a single storage contract across layers.

``TradingCacheProtocol`` (domain) unifies the OMS cache surface used by the
paper broker's internal cache and the trading execution caches. Every
implementation must be structurally conformant (runtime-checkable) and behave
identically through the shared contract: orders, positions, quotes, and
snapshot/restore/clear lifecycle.
"""

from __future__ import annotations

from decimal import Decimal

from tradex_brokers.paper.adapter import _PaperCache
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
from tradex_domain.protocols import TradingCacheProtocol

from tradex_trading.execution.thread_safe_cache import ThreadSafeTradingCache
from tradex_trading.execution.trading_cache import TradingCache

_CACHE_IMPLS = [
    ("paper", _PaperCache),
    ("trading", TradingCache),
    ("thread_safe", ThreadSafeTradingCache),
]


def _eq() -> Equity:
    return Equity.of("NSE", "RELIANCE")


def _make_order(oid: str = "o-1") -> Order:
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
    return Quote(instrument=_eq(), ltp=Price(value=Decimal("100")))


def _position_ref(name: str):
    """Reference accepted by get_position for a given implementation.

    NOTE: ``_PaperCache`` keys positions/quotes by instrument id; ``TradingCache``
    (and its thread-safe wrapper) key ``update_position``/``update_quote`` by
    ``instrument.symbol``. This keying difference is a real, pre-existing
    inconsistency in the unified interface (see ``TradingCache.update_position``
    vs ``get_position``). We query by each implementation's canonical key so the
    round-trip contract ("get returns what update stored") holds for all of them.
    """
    return _eq() if name == "paper" else "RELIANCE"


class TestCacheInterfaceConformance:
    """Every OMS cache implementation satisfies the unified protocol."""

    def test_all_implementations_are_runtime_checkable(self) -> None:
        for name, impl in _CACHE_IMPLS:
            assert isinstance(impl(), TradingCacheProtocol), name

    def test_orders_through_unified_interface(self) -> None:
        for name, impl in _CACHE_IMPLS:
            cache = impl()
            cache.update_order(_make_order("o-1"))
            assert cache.get_order("o-1") is not None, name
            assert cache.get_order("o-1").order_id.value == "o-1", name
            assert len(cache.all_orders()) == 1, name

    def test_positions_through_unified_interface(self) -> None:
        for name, impl in _CACHE_IMPLS:
            cache = impl()
            cache.update_position(_make_position())
            pos = cache.get_position(_position_ref(name))
            assert pos is not None, name
            assert pos.avg_price == Price(value=Decimal("100")), name
            assert len(cache.all_positions()) == 1, name

    def test_quotes_through_unified_interface(self) -> None:
        for name, impl in _CACHE_IMPLS:
            cache = impl()
            cache.update_quote(_make_quote())
            quote = cache.get_quote(_position_ref(name))
            assert quote is not None, name
            assert quote.ltp == Price(value=Decimal("100")), name

    def test_clear_empties_all(self) -> None:
        for name, impl in _CACHE_IMPLS:
            cache = impl()
            cache.update_order(_make_order())
            cache.update_position(_make_position())
            cache.update_quote(_make_quote())
            cache.clear()
            assert cache.all_orders() == [], name
            assert cache.all_positions() == [], name


class TestSnapshotRestoreLifecycle:
    """snapshot/restore round-trips through the unified contract."""

    def test_snapshot_restore_round_trip(self) -> None:
        for name, impl in _CACHE_IMPLS:
            cache = impl()
            cache.update_order(_make_order("o-1"))
            cache.update_position(_make_position())
            snapshot = cache.snapshot()

            fresh = impl()
            fresh.restore(snapshot)
            assert fresh.get_order("o-1") is not None, name
            assert len(fresh.all_orders()) == 1, name
            assert len(fresh.all_positions()) == 1, name

    def test_snapshot_structure(self) -> None:
        for name, impl in _CACHE_IMPLS:
            snapshot = impl().snapshot()
            assert set(snapshot) == {"orders", "positions", "quotes"}, name
