"""PaperFillSource / PaperBroker convergence test (GAP-3).

Documents and proves the convergence of the two paper fill paths for
the common case: MARKET orders with seeded LTP quotes.

**Intentional divergence:**
- ``PaperFillSource`` fills at LTP from the reactive cache (used by
  the ``ExecutionEngine`` pipeline). It participates in the unified
  ``FillModel`` price resolution shared with backtest/replay.
- ``PaperBroker`` fills at LTP, limit price, or by walking a
  multi-level order book (used as a standalone ``BrokerAdapter``).
  It simulates broker-specific order-book mechanics.

**Convergence:**
For MARKET orders with a seeded LTP quote, both paths fill at the
same price. This test proves that convergence.
"""

from __future__ import annotations

from datetime import UTC, datetime
from decimal import Decimal

from tradex_brokers.paper.adapter import PaperBroker
from tradex_domain import OrderSide
from tradex_domain.enums import OrderType
from tradex_domain.execution import OrderRequest
from tradex_domain.instruments import Equity
from tradex_domain.market import Quote
from tradex_domain.value_objects import Price, Quantity

from tradex_trading.execution.fill_sources import PaperFillSource
from tradex_trading.execution.trading_cache import TradingCache

INSTRUMENT = Equity.of("NSE", "RELIANCE")


class TestPaperFillConvergence:
    """PaperFillSource and PaperBroker fill at the same price for MARKET
    orders with seeded LTP quotes."""

    def test_market_order_fills_at_ltp_in_both_paths(self):
        """Both paths fill a MARKET BUY at the seeded LTP."""
        ltp = Price(value=Decimal("2500.50"))

        # Path A: PaperFillSource (reactive pipeline)
        cache = TradingCache()
        cache.update_quote(Quote(
            instrument=INSTRUMENT,
            ltp=ltp,
            bid=Price(value=Decimal("2500.00")),
            ask=Price(value=Decimal("2501.00")),
            timestamp=datetime(2026, 9, 1, tzinfo=UTC),
            exchange="NSE",
            provider="test",
        ))
        fill_source = PaperFillSource(cache=cache)
        request = OrderRequest(
            instrument=INSTRUMENT,
            side=OrderSide.BUY,
            order_type=OrderType.MARKET,
            quantity=Quantity(value=Decimal("10")),
        )
        _, fill_a = fill_source.submit(request)
        assert fill_a is not None
        price_a = fill_a.price.value

        # Path B: PaperBroker (standalone adapter)
        broker = PaperBroker(auto_fill=True)
        broker.set_quote(INSTRUMENT, ltp=ltp)
        broker.submit_order(request)
        positions = broker.get_positions()
        assert len(positions) == 1
        price_b = positions[0].avg_price.value

        # Convergence: both fill at LTP
        assert price_a == price_b == ltp.value, (
            f"PaperFillSource={price_a}, PaperBroker={price_b}, LTP={ltp.value}"
        )

    def test_market_sell_fills_at_ltp_in_both_paths(self):
        """Both paths fill a MARKET SELL at the seeded LTP."""
        ltp = Price(value=Decimal("1500.75"))

        # Path A: PaperFillSource
        cache = TradingCache()
        cache.update_quote(Quote(
            instrument=INSTRUMENT,
            ltp=ltp,
            bid=Price(value=Decimal("1500.00")),
            ask=Price(value=Decimal("1501.50")),
            timestamp=datetime(2026, 9, 1, tzinfo=UTC),
            exchange="NSE",
            provider="test",
        ))
        fill_source = PaperFillSource(cache=cache)
        request = OrderRequest(
            instrument=INSTRUMENT,
            side=OrderSide.SELL,
            order_type=OrderType.MARKET,
            quantity=Quantity(value=Decimal("5")),
        )
        _, fill_a = fill_source.submit(request)
        assert fill_a is not None
        price_a = fill_a.price.value

        # Path B: PaperBroker
        broker = PaperBroker(auto_fill=True)
        broker.set_quote(INSTRUMENT, ltp=ltp)
        broker.submit_order(request)
        positions = broker.get_positions()
        assert len(positions) == 1
        price_b = positions[0].avg_price.value

        assert price_a == price_b == ltp.value

    def test_limit_order_divergence_is_documented(self):
        """LIMIT orders diverge: PaperFillSource uses FillModel (request
        price), PaperBroker uses the order's limit price. This is the
        documented intentional divergence (GAP-3)."""
        limit = Price(value=Decimal("100"))
        ltp = Price(value=Decimal("99"))  # LTP below limit

        # PaperFillSource: FillModel resolves at LTP (market_price wins)
        cache = TradingCache()
        cache.update_quote(Quote(
            instrument=INSTRUMENT,
            ltp=ltp,
            bid=Price(value=Decimal("98.50")),
            ask=Price(value=Decimal("99.50")),
            timestamp=datetime(2026, 9, 1, tzinfo=UTC),
            exchange="NSE",
            provider="test",
        ))
        fill_source = PaperFillSource(cache=cache)
        request = OrderRequest(
            instrument=INSTRUMENT,
            side=OrderSide.BUY,
            order_type=OrderType.LIMIT,
            quantity=Quantity(value=Decimal("10")),
            price=limit,
        )
        _, fill = fill_source.submit(request)
        assert fill is not None
        # PaperFillSource fills at LTP (99), not limit (100)
        assert fill.price.value == ltp.value

        # PaperBroker: fills at limit price (100) for LIMIT orders
        broker = PaperBroker(auto_fill=True)
        broker.set_quote(INSTRUMENT, ltp=ltp)
        broker.submit_order(request)
        positions = broker.get_positions()
        assert len(positions) == 1
        # PaperBroker fills at the limit price
        assert positions[0].avg_price.value == limit.value

        # The divergence is documented: PaperFillSource uses FillModel
        # (LTP wins), PaperBroker uses the order's limit price.
