"""Gap tests for fill sources — PaperFillSource cache, BrokerFillSource, ReplayFillSource."""

from __future__ import annotations

from datetime import UTC, datetime
from decimal import Decimal
from unittest.mock import MagicMock

from tradex_domain.enums import OrderSide, OrderType, TimeInForce
from tradex_domain.execution import Fill, OrderRequest
from tradex_domain.instruments import Equity
from tradex_domain.market import Quote
from tradex_domain.value_objects import OrderId, Price, Quantity

from tradex_trading.execution.fill_sources import (
    BrokerFillSource,
    PaperFillSource,
    ReplayFillSource,
)


def _make_request(symbol: str = "TEST", price: str = "100") -> OrderRequest:
    instrument = Equity.of("NSE", symbol)
    return OrderRequest(
        instrument=instrument,
        side=OrderSide.BUY,
        order_type=OrderType.LIMIT,
        quantity=Quantity(value=Decimal("10")),
        price=Price(value=Decimal(price)),
        time_in_force=TimeInForce.DAY,
    )


def test_paper_fill_source_with_cache_ltp() -> None:
    """PaperFillSource uses LTP from cache quote when available."""
    instrument = Equity.of("NSE", "TEST")
    quote = Quote(instrument=instrument, ltp=Price(value=Decimal("150")))
    cache = MagicMock()
    cache.get_quote.return_value = quote
    source = PaperFillSource(cache=cache)
    req = _make_request(price="100")
    order, fill = source.submit(req)
    assert fill is not None
    # Should use cache LTP (150) not request price (100)
    assert fill.price.value == Decimal("150")


def test_broker_fill_source_position_projection_properties() -> None:
    """BrokerFillSource exposes boundary/projection metadata."""
    broker = MagicMock()
    broker.owns_position_projection = True
    broker.trading_cache = "mock_cache"
    source = BrokerFillSource(broker=broker)
    assert source.position_projection_owned is True
    assert source.position_projection_cache == "mock_cache"
    assert source.submission_boundary_crossed is False


def test_replay_fill_source_overrides_order_id() -> None:
    """ReplayFillSource assigns new order_id to replayed fill."""
    instrument = Equity.of("NSE", "REPLAY")
    historical_fill = Fill(
        order_id=OrderId(value="old-order-id"),
        instrument=instrument,
        side=OrderSide.BUY,
        quantity=Quantity(value=Decimal("5")),
        price=Price(value=Decimal("200")),
        timestamp=datetime.now(UTC),
    )
    source = ReplayFillSource(fills=[historical_fill])
    req = _make_request(symbol="REPLAY")
    order, fill = source.submit(req)
    assert fill is not None
    # Fill's order_id should match the new order, not the historical one
    assert fill.order_id == order.order_id
    assert fill.order_id.value != "old-order-id"
