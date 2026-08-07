"""Tests for Paper broker methods added for v3/v4 symmetry.

Covers: get_order_list, convert_position, exit_all, mass_status.
"""

from __future__ import annotations

from decimal import Decimal

import pytest
from tradex_domain.enums import OrderSide, OrderStatus, OrderType, ProductType
from tradex_domain.errors import CapabilityNotSupportedError
from tradex_domain.execution import OrderRequest
from tradex_domain.instruments import Equity, Index
from tradex_domain.value_objects import Price, Quantity

from tradex_brokers.paper.adapter import PaperBroker


class TestDepthGate:
    """Depth is NSE-only platform-wide (venue constraint)."""

    @pytest.mark.parametrize(
        "instrument",
        [
            Equity.of("MCX", "CRUDEOIL"),
            Equity.of("BSE", "RELIANCE"),
            Index.of("IDX", "NIFTY"),
        ],
    )
    def test_non_nse_depth_raises(self, instrument) -> None:
        broker = PaperBroker(auto_fill=False)
        with pytest.raises(CapabilityNotSupportedError, match="NSE"):
            broker.depth(instrument)

    def test_nse_depth_returns_book(self) -> None:
        broker = PaperBroker(auto_fill=False)
        eq = _eq()
        broker.set_quote(eq, ltp=Price(value=Decimal("100")))
        assert broker.depth(eq).instrument.instrument_id == eq.instrument_id


def _eq() -> Equity:
    return Equity.of("NSE", "RELIANCE")


def _make_request(instrument: Equity | None = None) -> OrderRequest:
    return OrderRequest(
        instrument=instrument or _eq(),
        side=OrderSide.BUY,
        order_type=OrderType.MARKET,
        quantity=Quantity(value=Decimal("10")),
        price=Price(value=Decimal("100")),
    )


class TestGetOrderList:
    """Tests for PaperBroker.get_order_list."""

    def test_returns_all_orders_by_default(self) -> None:
        broker = PaperBroker(auto_fill=False)
        eq = _eq()
        broker.set_quote(eq, ltp=Price(value=Decimal("100")))
        broker.submit_order(_make_request(eq))
        broker.submit_order(_make_request(eq))

        result = broker.get_order_list()
        assert len(result) == 2

    def test_filters_by_status(self) -> None:
        broker = PaperBroker(auto_fill=False)
        eq = _eq()
        broker.set_quote(eq, ltp=Price(value=Decimal("100")))
        broker.submit_order(_make_request(eq))

        # Order should be in ACK status
        ack_orders = broker.get_order_list(status="ACK")
        assert len(ack_orders) == 1

        # No FILLED orders since auto_fill is False
        filled = broker.get_order_list(status="FILLED")
        assert len(filled) == 0

    def test_empty_when_no_orders(self) -> None:
        broker = PaperBroker()
        result = broker.get_order_list()
        assert result == []


class TestConvertPosition:
    """Tests for PaperBroker.convert_position."""

    def test_returns_conversion_dict(self) -> None:
        broker = PaperBroker()
        eq = _eq()
        result = broker.convert_position(
            eq,
            from_product=ProductType.INTRADAY,
            to_product=ProductType.DELIVERY,
            quantity=10,
        )
        assert result["status"] == "converted"
        assert result["from_product"] == "INTRADAY"
        assert result["to_product"] == "DELIVERY"
        assert result["quantity"] == 10

    def test_includes_instrument_id(self) -> None:
        broker = PaperBroker()
        eq = _eq()
        result = broker.convert_position(
            eq,
            from_product=ProductType.DELIVERY,
            to_product=ProductType.INTRADAY,
            quantity=5,
        )
        assert "instrument" in result


class TestExitAll:
    """Tests for PaperBroker.exit_all."""

    def test_cancels_open_orders(self) -> None:
        broker = PaperBroker(auto_fill=False)
        eq = _eq()
        broker.set_quote(eq, ltp=Price(value=Decimal("100")))
        broker.submit_order(_make_request(eq))
        broker.submit_order(_make_request(eq))

        result = broker.exit_all()
        assert result["cancelled_orders"] == 2

        # All orders should now be cancelled
        for order in broker.get_orderbook():
            assert order.status is OrderStatus.CANCELLED

    def test_clears_positions(self) -> None:
        broker = PaperBroker(auto_fill=True)
        eq = _eq()
        broker.set_quote(eq, ltp=Price(value=Decimal("100")))
        broker.submit_order(_make_request(eq))
        assert len(broker.get_positions()) == 1

        broker.exit_all()
        assert len(broker.get_positions()) == 0

    def test_returns_dict_with_counts(self) -> None:
        broker = PaperBroker()
        result = broker.exit_all()
        assert "cancelled_orders" in result
        assert "flattened_positions" in result


class TestMassStatus:
    """Tests for PaperBroker.mass_status."""

    def test_returns_orders_positions_account(self) -> None:
        broker = PaperBroker()
        result = broker.mass_status()
        assert "orders" in result
        assert "positions" in result
        assert "account" in result

    def test_includes_submitted_orders(self) -> None:
        broker = PaperBroker(auto_fill=False)
        eq = _eq()
        broker.set_quote(eq, ltp=Price(value=Decimal("100")))
        broker.submit_order(_make_request(eq))

        result = broker.mass_status()
        assert len(result["orders"]) == 1

    def test_account_has_balance(self) -> None:
        broker = PaperBroker(starting_cash=Decimal("50000"))
        result = broker.mass_status()
        assert result["account"].balance.amount == Decimal("50000")
