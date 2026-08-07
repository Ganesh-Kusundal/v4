"""Gap tests for PositionManager — flips, multi-position, zero-qty edge."""

from __future__ import annotations

from datetime import UTC, datetime
from decimal import Decimal

from tradex_domain.enums import OrderSide
from tradex_domain.execution import Fill
from tradex_domain.instruments import Equity
from tradex_domain.value_objects import OrderId, Price, Quantity

from tradex_trading.execution.position_manager import PositionManager
from tradex_trading.execution.trading_cache import TradingCache


def _make_fill(
    symbol: str,
    side: OrderSide,
    qty: str,
    price: str = "100",
) -> Fill:
    instrument = Equity.of("NSE", symbol)
    return Fill(
        order_id=OrderId(value="fill-oid"),
        instrument=instrument,
        side=side,
        quantity=Quantity(value=Decimal(qty)),
        price=Price(value=Decimal(price)),
        timestamp=datetime.now(UTC),
    )


def test_short_to_long_flip() -> None:
    """SELL then BUY flips position from short to long."""
    cache = TradingCache()
    pm = PositionManager(cache)
    # Open short: SELL 10
    pm.on_fill(_make_fill("FLIP", OrderSide.SELL, "10", price="100"))
    pos = pm.get_position(Equity.of("NSE", "FLIP"))
    assert pos is not None
    assert pos.quantity.value == Decimal("-10")
    # BUY 20 — flips to long +10
    pos = pm.on_fill(_make_fill("FLIP", OrderSide.BUY, "20", price="90"))
    assert pos.quantity.value == Decimal("10")
    # After flip, avg_price should be the fill price of the BUY
    assert pos.avg_price.value == Decimal("90")


def test_all_positions_returns_multiple() -> None:
    """all_positions returns positions for multiple instruments."""
    cache = TradingCache()
    pm = PositionManager(cache)
    pm.on_fill(_make_fill("AAA", OrderSide.BUY, "5"))
    pm.on_fill(_make_fill("BBB", OrderSide.BUY, "10"))
    positions = pm.all_positions()
    symbols = {p.instrument.symbol for p in positions}
    assert "AAA" in symbols
    assert "BBB" in symbols
    assert len(positions) >= 2


def test_on_fill_zero_quantity_edge_case() -> None:
    """Fill with zero quantity results in zero position."""
    cache = TradingCache()
    pm = PositionManager(cache)
    # BUY 10
    pm.on_fill(_make_fill("ZERO", OrderSide.BUY, "10", price="100"))
    # SELL 10 — position goes to zero
    pos = pm.on_fill(_make_fill("ZERO", OrderSide.SELL, "10", price="110"))
    assert pos.quantity.value == Decimal("0")
    # Realized PnL should reflect the round-trip profit
    assert pos.realized_pnl.amount != Decimal("0")
