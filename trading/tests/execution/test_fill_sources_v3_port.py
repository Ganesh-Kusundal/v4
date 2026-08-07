"""Tests for v3-ported BrokerFillSource properties."""

from __future__ import annotations

from decimal import Decimal
from unittest.mock import MagicMock

from tradex_domain.enums import OrderSide, OrderType, ProductType, TimeInForce
from tradex_domain.execution import OrderRequest
from tradex_domain.instruments import Equity
from tradex_domain.value_objects import Price, Quantity

from tradex_trading.execution.fill_sources import BrokerFillSource


def _make_request(price: Decimal | None = None) -> OrderRequest:
    return OrderRequest(
        instrument=Equity.of("NSE", "RELIANCE"),
        side=OrderSide.BUY,
        order_type=OrderType.LIMIT if price else OrderType.MARKET,
        quantity=Quantity(value=Decimal("10")),
        price=Price(value=price) if price else None,
        time_in_force=TimeInForce.DAY,
        product_type=ProductType.INTRADAY,
    )


class TestBrokerFillSourceProperties:
    """BrokerFillSource boundary/projection metadata properties."""

    def test_submission_boundary_not_crossed_initially(self) -> None:
        bfs = BrokerFillSource(MagicMock())
        assert bfs.submission_boundary_crossed is False

    def test_submission_boundary_crossed_after_submit(self) -> None:
        broker = MagicMock()
        broker.submit_order.return_value = "order-id"
        bfs = BrokerFillSource(broker)
        bfs.submit(_make_request(price=Decimal("100")))
        assert bfs.submission_boundary_crossed is True

    def test_position_projection_owned_default_false(self) -> None:
        broker = MagicMock(spec=[])  # no attributes
        bfs = BrokerFillSource(broker)
        assert bfs.position_projection_owned is False

    def test_position_projection_owned_when_adapter_declares(self) -> None:
        broker = MagicMock()
        broker.owns_position_projection = True
        bfs = BrokerFillSource(broker)
        assert bfs.position_projection_owned is True

    def test_position_projection_cache_default_none(self) -> None:
        broker = MagicMock(spec=[])
        bfs = BrokerFillSource(broker)
        assert bfs.position_projection_cache is None

    def test_position_projection_cache_returns_adapter_cache(self) -> None:
        broker = MagicMock()
        sentinel = object()
        broker.trading_cache = sentinel
        bfs = BrokerFillSource(broker)
        assert bfs.position_projection_cache is sentinel
