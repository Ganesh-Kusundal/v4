"""Tests for fill sources — BrokerFillSource, PaperFillSource, SimulatedFillSource."""

from __future__ import annotations

from decimal import Decimal
from unittest.mock import MagicMock

import pytest
from tradex_domain.enums import OrderSide, OrderStatus, OrderType, ProductType, TimeInForce
from tradex_domain.errors import OrderRejectedError
from tradex_domain.execution import BracketOrderRequest, OrderRequest
from tradex_domain.instruments import Equity
from tradex_domain.value_objects import OrderId, Price, Quantity

from tradex_trading.execution.fill_sources import (
    BrokerFillSource,
    PaperFillSource,
    SimulatedFillSource,
)


def _make_request(
    price: Decimal | None = None,
    quantity: Decimal = Decimal("10"),
) -> OrderRequest:
    return OrderRequest(
        instrument=Equity.of("NSE", "RELIANCE"),
        side=OrderSide.BUY,
        order_type=OrderType.LIMIT if price else OrderType.MARKET,
        quantity=Quantity(value=quantity),
        price=Price(value=price) if price else None,
        time_in_force=TimeInForce.DAY,
        product_type=ProductType.INTRADAY,
    )


class TestSimulatedFillSource:
    """SimulatedFillSource fills at request price; rejects price-less orders."""

    def test_fill_at_limit_price(self) -> None:
        fill_source = SimulatedFillSource()
        req = _make_request(price=Decimal("2500.00"))
        order, fill = fill_source.submit(req)
        assert order.status == OrderStatus.FILLED
        assert fill is not None
        assert fill.price.value == Decimal("2500.00")

    def test_price_less_market_order_raises(self) -> None:
        """A zero-priced fill silently corrupts P&L (avg_price=0) and breaks
        FeeCalculator — SimulatedFillSource must fail loudly instead."""
        fill_source = SimulatedFillSource()
        req = _make_request()  # MARKET, no price
        with pytest.raises(ValueError, match="without a positive price"):
            fill_source.submit(req)

    def test_fill_uses_trigger_price_as_fallback(self) -> None:
        fill_source = SimulatedFillSource()
        req = OrderRequest(
            instrument=Equity.of("NSE", "RELIANCE"),
            side=OrderSide.BUY,
            order_type=OrderType.MARKET,
            quantity=Quantity(value=Decimal("10")),
            price=None,
            trigger_price=Price(value=Decimal("100.50")),
            time_in_force=TimeInForce.DAY,
            product_type=ProductType.INTRADAY,
        )
        order, fill = fill_source.submit(req)
        assert fill is not None
        assert fill.price.value == Decimal("100.50")


class TestPaperFillSource:
    """PaperFillSource fills at LTP, request price, or nominal."""

    def test_price_less_market_order_raises(self) -> None:
        """PaperFillSource must reject price-less MARKET orders just like
        SimulatedFillSource — a zero-priced fill corrupts P&L."""
        fill_source = PaperFillSource()
        req = _make_request()  # MARKET, no price
        with pytest.raises(ValueError, match="without a positive price"):
            fill_source.submit(req)

    def test_fill_at_request_price(self) -> None:
        fill_source = PaperFillSource()
        req = _make_request(price=Decimal("3000.00"))
        order, fill = fill_source.submit(req)
        assert fill is not None
        assert fill.price.value == Decimal("3000.00")


class TestBrokerFillSource:
    """BrokerFillSource delegates to broker adapter's submit_order."""

    def test_calls_submit_order(self) -> None:
        mock_broker = MagicMock()
        mock_broker.submit_order.return_value = "order-123"
        fill_source = BrokerFillSource(mock_broker)

        req = _make_request(price=Decimal("2500.00"))
        order, fill = fill_source.submit(req)

        mock_broker.submit_order.assert_called_once_with(req)
        assert order.status == OrderStatus.ACK
        assert fill is None

    def test_does_not_call_place_order(self) -> None:
        """BrokerFillSource must NOT call place_order (old bug)."""
        mock_broker = MagicMock()
        mock_broker.submit_order.return_value = "order-123"
        fill_source = BrokerFillSource(mock_broker)

        req = _make_request(price=Decimal("2500.00"))
        fill_source.submit(req)

        mock_broker.place_order.assert_not_called()

    def test_fallback_when_no_submit_order(self) -> None:
        """If broker has no submit_order, returns ACK order."""
        plain_broker = object()  # no submit_order method
        fill_source = BrokerFillSource(plain_broker)

        req = _make_request(price=Decimal("2500.00"))
        order, fill = fill_source.submit(req)
        assert order.status == OrderStatus.ACK
        assert fill is None

    def test_bracket_request_calls_submit_super_order(self) -> None:
        """A BracketOrderRequest must hit the broker's super-order endpoint,
        never the plain submit_order (a bracket submitted as a bare entry
        would silently lose its protective legs)."""
        mock_broker = MagicMock()
        mock_broker.submit_super_order.return_value = "super-1"
        fill_source = BrokerFillSource(mock_broker)

        req = BracketOrderRequest(
            instrument=Equity.of("NSE", "RELIANCE"),
            side=OrderSide.BUY,
            order_type=OrderType.LIMIT,
            quantity=Quantity(value=Decimal("10")),
            price=Price(value=Decimal("2500.00")),
            stop_loss_price=Price(value=Decimal("2450.00")),
            target_price=Price(value=Decimal("2600.00")),
            time_in_force=TimeInForce.DAY,
        )
        order, fill = fill_source.submit(req)

        mock_broker.submit_super_order.assert_called_once_with(req)
        mock_broker.submit_order.assert_not_called()
        assert order.status == OrderStatus.ACK
        assert order.order_id.value == "super-1"
        assert fill is None

    def test_bracket_request_rejected_when_broker_lacks_super_orders(self) -> None:
        """A broker with no super-order endpoint must fail loudly, not degrade
        to an unprotected entry submission."""
        mock_broker = MagicMock()
        del mock_broker.submit_super_order  # broker without the method
        fill_source = BrokerFillSource(mock_broker)

        req = BracketOrderRequest(
            instrument=Equity.of("NSE", "RELIANCE"),
            side=OrderSide.BUY,
            order_type=OrderType.LIMIT,
            quantity=Quantity(value=Decimal("10")),
            price=Price(value=Decimal("2500.00")),
            stop_loss_price=Price(value=Decimal("2450.00")),
            target_price=Price(value=Decimal("2600.00")),
            time_in_force=TimeInForce.DAY,
        )
        with pytest.raises(ValueError, match="does not support super orders"):
            fill_source.submit(req)
        mock_broker.submit_order.assert_not_called()

    def test_bracket_modify_calls_submit_super_order_sibling(self) -> None:
        """Modifying a bracket must reach modify_super_order — sending the
        composite id to the plain modify endpoint would drop its legs."""
        mock_broker = MagicMock()
        fill_source = BrokerFillSource(mock_broker)
        req = BracketOrderRequest(
            instrument=Equity.of("NSE", "RELIANCE"),
            side=OrderSide.BUY,
            order_type=OrderType.LIMIT,
            quantity=Quantity(value=Decimal("10")),
            price=Price(value=Decimal("2500.00")),
            stop_loss_price=Price(value=Decimal("2450.00")),
            target_price=Price(value=Decimal("2600.00")),
            time_in_force=TimeInForce.DAY,
        )
        oid = OrderId(value="super-1")
        fill_source.modify(oid, req)

        mock_broker.modify_super_order.assert_called_once_with(oid, req)
        mock_broker.modify_order.assert_not_called()

    def test_bracket_modify_plain_order_uses_plain_endpoint(self) -> None:
        """A non-bracket modification keeps using modify_order."""
        mock_broker = MagicMock()
        fill_source = BrokerFillSource(mock_broker)
        req = _make_request(price=Decimal("2500.00"))
        oid = OrderId(value="o-1")
        fill_source.modify(oid, req)

        mock_broker.modify_order.assert_called_once_with(oid, req)

    def test_broker_cancel_super_order(self) -> None:
        """cancel_super_order delegates to the broker's super endpoint."""
        mock_broker = MagicMock()
        fill_source = BrokerFillSource(mock_broker)
        oid = OrderId(value="super-1")
        fill_source.cancel_super_order(oid)

        mock_broker.cancel_super_order.assert_called_once_with(oid)

    def test_broker_cancel_super_order_fails_loudly_without_endpoint(self) -> None:
        """A broker without super-order cancellation must raise, never fall
        back to a plain cancel on the composite id."""
        mock_broker = MagicMock()
        del mock_broker.cancel_super_order
        fill_source = BrokerFillSource(mock_broker)

        with pytest.raises(OrderRejectedError, match="does not support super-order"):
            fill_source.cancel_super_order(OrderId(value="super-1"))
        mock_broker.cancel_order.assert_not_called()
