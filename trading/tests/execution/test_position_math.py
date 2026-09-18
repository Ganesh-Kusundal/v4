"""Contract tests for the shared position-accounting model (CRITICAL-1).

``execution.position_math.apply_fill`` is the single weighted-average +
realized-PnL model used by both the reactive pipeline (PositionManager) and
BacktestEngine. These tests pin its contract directly so the shared model is
a first-class, auditable artifact — and prove that identical fill streams
produce identical Position state through either accounting engine.
"""

from __future__ import annotations

from datetime import UTC, datetime
from decimal import Decimal

from tradex_domain.enums import OrderSide
from tradex_domain.execution import Fill
from tradex_domain.instruments import Equity
from tradex_domain.value_objects import Money, OrderId, Price, Quantity

from tradex_trading.execution.position_manager import PositionManager
from tradex_domain.position_math import apply_fill
from tradex_trading.execution.trading_cache import TradingCache


def _fill(side: OrderSide, qty: str, price: str) -> Fill:
    return Fill(
        order_id=OrderId(value="pm-oid"),
        instrument=Equity.of("NSE", "SHARED"),
        side=side,
        quantity=Quantity(value=Decimal(qty)),
        price=Price(value=Decimal(price)),
        timestamp=datetime(2026, 8, 1, tzinfo=UTC),
    )


class TestSharedModelContract:
    """apply_fill is the pure, immutable single-source accounting math."""

    def test_open_long_weighted_average(self) -> None:
        pos = apply_fill(None, _fill(OrderSide.BUY, "2", "100"))
        pos = apply_fill(pos, _fill(OrderSide.BUY, "1", "110"))
        assert pos.quantity.value == Decimal("3")
        # (100*2 + 110) / 3 = 103.333… → quantized to 103.33 (H4 fix).
        assert pos.avg_price.value == Decimal("103.33")
        assert pos.realized_pnl.amount == Decimal("0")

    def test_partial_close_books_realized_pnl(self) -> None:
        pos = apply_fill(None, _fill(OrderSide.BUY, "3", "100"))
        pos = apply_fill(pos, _fill(OrderSide.SELL, "1", "120"))
        assert pos.quantity.value == Decimal("2")
        # Closed 1 @ (120 - 100) = 20.
        assert pos.realized_pnl.amount == Decimal("20")
        # Remaining qty keeps the original average.
        assert pos.avg_price.value == Decimal("100")

    def test_full_close_realizes_round_trip(self) -> None:
        pos = apply_fill(None, _fill(OrderSide.BUY, "2", "100"))
        pos = apply_fill(pos, _fill(OrderSide.SELL, "2", "110"))
        assert pos.quantity.value == Decimal("0")
        assert pos.realized_pnl.amount == Decimal("20")

    def test_flip_rebases_average_at_fill_price(self) -> None:
        pos = apply_fill(None, _fill(OrderSide.SELL, "10", "100"))
        pos = apply_fill(pos, _fill(OrderSide.BUY, "20", "90"))
        assert pos.quantity.value == Decimal("10")
        # Flip re-bases at the buy fill price; shorts realize on the close.
        assert pos.avg_price.value == Decimal("90")
        # (100 - 90) * 10 closed short units = 100 realized.
        assert pos.realized_pnl.amount == Decimal("100")

    def test_short_profit_books_positive_realized(self) -> None:
        pos = apply_fill(None, _fill(OrderSide.SELL, "5", "100"))
        pos = apply_fill(pos, _fill(OrderSide.BUY, "5", "90"))
        assert pos.quantity.value == Decimal("0")
        assert pos.realized_pnl.amount == Decimal("50")

    def test_non_integer_average_quantizes_realized_to_paisa(self) -> None:
        """Average price is paisa-quantized (ROUND_HALF_UP) on every fill
        (H4 fix), so a non-terminating average (310/3) becomes 103.33
        immediately — and the realized P&L accumulates from the quantized
        base."""
        pos = apply_fill(None, _fill(OrderSide.BUY, "1", "100"))
        pos = apply_fill(pos, _fill(OrderSide.BUY, "2", "105"))
        # (100*1 + 105*2) / 3 = 310/3 = 103.333… → quantized to 103.33
        assert pos.avg_price.value == Decimal("103.33")
        # SELL 1 @110 → delta (110 - 103.33) = 6.67
        pos = apply_fill(pos, _fill(OrderSide.SELL, "1", "110"))
        assert pos.realized_pnl.amount == Decimal("6.67")
        # SELL 2 @112 → delta (112 - 103.33) * 2 = 17.34; accumulated 24.01
        pos = apply_fill(pos, _fill(OrderSide.SELL, "2", "112"))
        assert pos.quantity.value == Decimal("0")
        # Quantized round trip: proceeds 334 − cost 310 = 24, but the
        # quantized avg shifts the per-fill deltas slightly: 6.67 + 17.34 = 24.01
        assert pos.realized_pnl.amount == Decimal("24.01")

    def test_input_is_never_mutated(self) -> None:
        pos = apply_fill(None, _fill(OrderSide.BUY, "1", "100"))
        original_qty = pos.quantity.value
        apply_fill(pos, _fill(OrderSide.SELL, "1", "110"))
        assert pos.quantity.value == original_qty


class TestSharedModelUsedByBothEngines:
    """PositionManager (reactive/live) and apply_fill are one model — the
    same fill stream produces the same Position state through both paths."""

    def test_position_manager_delegates_to_shared_model(self) -> None:
        cache = TradingCache()
        pm = PositionManager(cache)
        pm.on_fill(_fill(OrderSide.BUY, "2", "100"))
        pm.on_fill(_fill(OrderSide.BUY, "1", "110"))
        pm.on_fill(_fill(OrderSide.SELL, "1", "120"))
        pos = pm.get_position(Equity.of("NSE", "SHARED"))
        assert pos is not None

        expected = apply_fill(
            apply_fill(
                apply_fill(None, _fill(OrderSide.BUY, "2", "100")),
                _fill(OrderSide.BUY, "1", "110"),
            ),
            _fill(OrderSide.SELL, "1", "120"),
        )
        assert pos.quantity.value == expected.quantity.value
        assert pos.avg_price.value == expected.avg_price.value
        assert pos.realized_pnl.amount == expected.realized_pnl.amount

    def test_on_fee_deducts_from_realized_pnl(self) -> None:
        """PositionManager.on_fee makes reactive net P&L consistent with
        BacktestEngine's net cash accounting (HIGH-6b)."""
        cache = TradingCache()
        pm = PositionManager(cache)
        pm.on_fill(_fill(OrderSide.BUY, "10", "100"))
        pos = pm.on_fee(_fill(OrderSide.BUY, "10", "100"), Money(amount=Decimal("5.50")))
        assert pos is not None
        # Position untouched; fees shave realized P&L, paisa-quantized.
        assert pos.quantity.value == Decimal("10")
        assert pos.avg_price.value == Decimal("100")
        assert pos.realized_pnl.amount == Decimal("-5.50")

    def test_on_fee_no_position_is_safe(self) -> None:
        cache = TradingCache()
        pm = PositionManager(cache)
        assert pm.on_fee(_fill(OrderSide.BUY, "10", "100"), Money(amount=Decimal("1"))) is None
