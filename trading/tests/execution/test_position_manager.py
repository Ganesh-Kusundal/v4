"""WS-E contract tests: PositionManager — ported from v3.

Adapted for v4 API: on_fill() (not apply_fill()), no apply() method,
cache uses string-keyed positions.
"""

from __future__ import annotations

from datetime import UTC, datetime
from decimal import Decimal

from tradex_domain import (
    Equity,
    Fill,
    OrderId,
    OrderSide,
    Price,
    Quantity,
)

from tradex_trading.execution.position_manager import PositionManager
from tradex_trading.execution.trading_cache import TradingCache


def _eq() -> Equity:
    return Equity.of("NSE", "RELIANCE")


def _fill(order_id: str, side: OrderSide, qty: int, price: int) -> Fill:
    return Fill(
        order_id=OrderId(value=order_id),
        instrument=_eq(),
        side=side,
        quantity=Quantity(value=Decimal(qty)),
        price=Price(value=Decimal(price)),
        timestamp=datetime(2026, 8, 1, tzinfo=UTC),
    )


def _manager() -> tuple[PositionManager, TradingCache]:
    cache = TradingCache()
    return PositionManager(cache), cache


def test_open_long_weighted_average() -> None:
    pm, _cache = _manager()
    pm.on_fill(_fill("f1", OrderSide.BUY, 10, 100))
    pm.on_fill(_fill("f2", OrderSide.BUY, 10, 120))
    pos = pm.get_position(_eq())
    assert pos is not None
    assert pos.quantity.value == 20
    assert pos.avg_price.value == 110
    assert pos.realized_pnl.amount == 0


def test_reduce_realizes_pnl() -> None:
    pm, _cache = _manager()
    pm.on_fill(_fill("f1", OrderSide.BUY, 10, 100))
    pos = pm.on_fill(_fill("f2", OrderSide.SELL, 4, 130))
    assert pos.quantity.value == 6
    assert pos.avg_price.value == 100
    assert pos.realized_pnl.amount == (130 - 100) * 4


def test_close_flat_keeps_avg_price_in_v4() -> None:
    pm, _cache = _manager()
    pm.on_fill(_fill("f1", OrderSide.BUY, 10, 100))
    pos = pm.on_fill(_fill("f2", OrderSide.SELL, 10, 120))
    assert pos.quantity.value == 0
    # v4 keeps the old avg_price when position goes flat (unlike v3 which zeroed it)
    assert pos.avg_price.value == 100
    assert pos.realized_pnl.amount == (120 - 100) * 10


def test_flip_leftover_opens_at_trade_price() -> None:
    pm, _cache = _manager()
    pm.on_fill(_fill("f1", OrderSide.BUY, 10, 100))
    pos = pm.on_fill(_fill("f2", OrderSide.SELL, 15, 110))
    assert pos.quantity.value == -5
    assert pos.avg_price.value == 110
    assert pos.realized_pnl.amount == (110 - 100) * 10


def test_short_open_and_reduce() -> None:
    pm, _cache = _manager()
    pm.on_fill(_fill("f1", OrderSide.SELL, 5, 100))
    pos = pm.on_fill(_fill("f2", OrderSide.BUY, 3, 90))
    assert pos.quantity.value == -2
    assert pos.avg_price.value == 100
    assert pos.realized_pnl.amount == (100 - 90) * 3


def test_position_reflected_in_cache() -> None:
    pm, cache = _manager()
    pm.on_fill(_fill("f1", OrderSide.BUY, 10, 100))
    cached = cache.get_position(_eq())
    assert cached is not None
    assert cached.quantity.value == 10
    assert cached.avg_price.value == 100
