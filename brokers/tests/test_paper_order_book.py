"""Tests for PaperBroker order-book simulation (Phase 5.3)."""

from __future__ import annotations

from decimal import Decimal

from tradex_domain import Equity, OrderRequest, OrderSide, OrderStatus, OrderType, Price, Quantity

from tradex_brokers.paper.adapter import PaperBroker


def _eq() -> Equity:
    return Equity.of("NSE", "RELIANCE")


class TestPaperOrderBook:
    """Order-book mode: multi-level matching, partial fills, price improvement."""

    def test_limit_buy_fills_against_ask(self) -> None:
        broker = PaperBroker(auto_fill=True, order_book=True)
        eq = _eq()
        broker.set_depth(
            eq,
            bids=[(Price(Decimal("99.90")), Quantity(Decimal("100")))],
            asks=[(Price(Decimal("100.10")), Quantity(Decimal("50")))],
        )
        req = OrderRequest(
            instrument=eq,
            side=OrderSide.BUY,
            order_type=OrderType.LIMIT,
            quantity=Quantity(Decimal("30")),
            price=Price(Decimal("100.10")),
        )
        oid = broker.submit_order(req)
        order = broker.get_order(oid)
        assert order.status is OrderStatus.FILLED
        assert order.filled_quantity.value == Decimal("30")
        assert order.price.value == Decimal("100.10")

    def test_limit_buy_rests_when_price_not_met(self) -> None:
        broker = PaperBroker(auto_fill=True, order_book=True)
        eq = _eq()
        broker.set_depth(
            eq,
            bids=[(Price(Decimal("99.90")), Quantity(Decimal("100")))],
            asks=[(Price(Decimal("100.50")), Quantity(Decimal("50")))],
        )
        req = OrderRequest(
            instrument=eq,
            side=OrderSide.BUY,
            order_type=OrderType.LIMIT,
            quantity=Quantity(Decimal("30")),
            price=Price(Decimal("100.00")),
        )
        oid = broker.submit_order(req)
        order = broker.get_order(oid)
        assert order.status is OrderStatus.ACK

    def test_partial_fill_across_levels(self) -> None:
        broker = PaperBroker(auto_fill=True, order_book=True)
        eq = _eq()
        broker.set_depth(
            eq,
            bids=[],
            asks=[
                (Price(Decimal("100.00")), Quantity(Decimal("20"))),
                (Price(Decimal("100.10")), Quantity(Decimal("30"))),
            ],
        )
        req = OrderRequest(
            instrument=eq,
            side=OrderSide.BUY,
            order_type=OrderType.LIMIT,
            quantity=Quantity(Decimal("40")),
            price=Price(Decimal("100.10")),
        )
        oid = broker.submit_order(req)
        order = broker.get_order(oid)
        assert order.status is OrderStatus.FILLED
        assert order.filled_quantity.value == Decimal("40")
        # M4 fix: order.price preserves the original limit price (100.10).
        # The weighted-average fill price is in avg_price_traded.
        # Weighted avg: (20*100 + 20*100.10) / 40 = 100.05
        assert order.price.value == Decimal("100.10")
        assert order.avg_price_traded is not None
        assert order.avg_price_traded.value == Decimal("100.05")

    def test_market_buy_walks_book(self) -> None:
        broker = PaperBroker(auto_fill=True, order_book=True)
        eq = _eq()
        broker.set_depth(
            eq,
            bids=[],
            asks=[
                (Price(Decimal("100.00")), Quantity(Decimal("10"))),
                (Price(Decimal("100.50")), Quantity(Decimal("10"))),
            ],
        )
        req = OrderRequest(
            instrument=eq,
            side=OrderSide.BUY,
            order_type=OrderType.MARKET,
            quantity=Quantity(Decimal("15")),
        )
        oid = broker.submit_order(req)
        order = broker.get_order(oid)
        assert order.status is OrderStatus.FILLED
        assert order.filled_quantity.value == Decimal("15")
        # (10*100 + 5*100.50) / 15 = 100.1666...
        assert order.price.value > Decimal("100.00")

    def test_set_depth_triggers_resting_order(self) -> None:
        broker = PaperBroker(auto_fill=True, order_book=True)
        eq = _eq()
        # Set initial depth with asks above the limit price
        broker.set_depth(
            eq,
            bids=[(Price(Decimal("99.00")), Quantity(Decimal("50")))],
            asks=[(Price(Decimal("101.00")), Quantity(Decimal("50")))],
        )
        req = OrderRequest(
            instrument=eq,
            side=OrderSide.BUY,
            order_type=OrderType.LIMIT,
            quantity=Quantity(Decimal("10")),
            price=Price(Decimal("100.00")),
        )
        oid = broker.submit_order(req)
        order = broker.get_order(oid)
        assert order.status is OrderStatus.ACK

        # Now set depth with asks below the limit — should trigger fill
        broker.set_depth(
            eq,
            bids=[],
            asks=[(Price(Decimal("99.90")), Quantity(Decimal("50")))],
        )
        order = broker.get_order(oid)
        assert order.status is OrderStatus.FILLED
