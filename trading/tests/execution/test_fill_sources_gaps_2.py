"""Gap tests for fill sources — PaperFillSource cache, BrokerFillSource."""

from __future__ import annotations

from decimal import Decimal
from unittest.mock import MagicMock

from tradex_domain.enums import OrderSide, OrderType, TimeInForce
from tradex_domain.execution import OrderRequest
from tradex_domain.instruments import Equity
from tradex_domain.market import Quote
from tradex_domain.value_objects import OrderId, Price, Quantity
from tradex_execution.fill_sources import (
    BrokerFillSource,
    PaperFillSource,
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
    # H7: LIMIT 100 with LTP 150 would clamp to 100; use limit 200 so LTP < limit
    req = _make_request(price="200")
    order, fill = source.submit(req)
    assert fill is not None
    # Should use cache LTP (150) not request price (200)
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
