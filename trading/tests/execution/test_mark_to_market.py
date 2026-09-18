"""Live mark-to-market and fail-closed risk contracts."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from decimal import Decimal

from tradex_domain.enums import OrderSide, OrderType, TimeInForce
from tradex_domain.execution import Fill, OrderRequest, Position
from tradex_domain.instruments import Equity
from tradex_domain.market import Quote
from tradex_domain.value_objects import OrderId, Price, Quantity

from tradex_trading.execution.risk import RiskManager
from tradex_trading.execution.mark_to_market import MarkToMarketService
from tradex_domain.position_math import apply_fill
from tradex_trading.execution.trading_cache import TradingCache
from tradex_trading.reactive.bus import ReactiveBus

_INSTRUMENT = Equity.of("NSE", "RELIANCE")
_MARKED_AT = datetime(2026, 9, 4, 10, 0, tzinfo=UTC)


def _fill(side: OrderSide, quantity: str = "10", price: str = "100") -> Fill:
    return Fill(
        order_id=OrderId(value="fill-1"),
        instrument=_INSTRUMENT,
        side=side,
        quantity=Quantity(Decimal(quantity)),
        price=Price(Decimal(price)),
        timestamp=_MARKED_AT,
    )


def _request(side: OrderSide = OrderSide.BUY) -> OrderRequest:
    return OrderRequest(
        instrument=_INSTRUMENT,
        side=side,
        order_type=OrderType.MARKET,
        quantity=Quantity(Decimal("1")),
        time_in_force=TimeInForce.DAY,
    )


def _position(side: OrderSide = OrderSide.BUY) -> Position:
    return apply_fill(None, _fill(side))


def _quote(
    timestamp: datetime = _MARKED_AT,
    *,
    ltp: str = "110",
    bid: str | None = "109",
    ask: str | None = "111",
) -> Quote:
    return Quote(
        instrument=_INSTRUMENT,
        ltp=Price(Decimal(ltp)),
        bid=Price(Decimal(bid)) if bid is not None else None,
        ask=Price(Decimal(ask)) if ask is not None else None,
        timestamp=timestamp,
    )


def test_quote_stream_marks_long_at_bid_and_updates_unrealized_pnl() -> None:
    cache = TradingCache()
    cache.update_position(_position(OrderSide.BUY))
    bus = ReactiveBus()
    service = MarkToMarketService(cache, bus)
    try:
        bus.publish(_quote())
        position = cache.get_position(_INSTRUMENT)
        assert position is not None
        assert position.mark_price == Price(Decimal("109"))
        assert position.mark_source == "BID"
        assert position.marked_at == _MARKED_AT
        assert position.unrealized_pnl.amount == Decimal("90")
    finally:
        service.close()


def test_quote_stream_marks_short_at_ask() -> None:
    cache = TradingCache()
    cache.update_position(_position(OrderSide.SELL))
    service = MarkToMarketService(cache)
    updated = service.on_quote(_quote())
    assert updated is not None
    assert updated.mark_price == Price(Decimal("111"))
    assert updated.mark_source == "ASK"
    # Short 10 @ 100 marked at 111 loses 110.
    assert updated.unrealized_pnl.amount == Decimal("-110")


def test_stale_quote_cannot_overwrite_newer_mark() -> None:
    cache = TradingCache()
    cache.update_position(_position())
    service = MarkToMarketService(cache)
    assert service.on_quote(_quote()) is not None
    stale = _quote(_MARKED_AT - timedelta(seconds=1), ltp="80", bid="79", ask="81")
    assert service.on_quote(stale) is None
    position = cache.get_position(_INSTRUMENT)
    assert position is not None
    assert position.marked_at == _MARKED_AT
    assert position.mark_price == Price(Decimal("109"))


def test_live_risk_fails_closed_without_fresh_mark_but_allows_reduction() -> None:
    positions = [_position()]
    quotes: dict[str, Quote] = {}
    risk = RiskManager(
        require_fresh_marks=True,
        max_mark_age_seconds=5,
        positions_provider=lambda: positions,
        price_provider=lambda instrument: quotes.get(str(instrument.instrument_id)),
    )
    # There is an open position but no quote-derived mark yet.
    assert risk.check(_request(OrderSide.BUY), now=_MARKED_AT) is False

    quotes[str(_INSTRUMENT.instrument_id)] = _quote()
    # The existing position is still unmarked; the live gate remains closed.
    assert risk.check(_request(OrderSide.BUY), now=_MARKED_AT) is False
    marked = TradingCache()
    marked.update_position(_position())
    marking = MarkToMarketService(marked)
    positions[0] = marking.on_quote(_quote()) or positions[0]
    marking.close()
    assert risk.check(_request(OrderSide.BUY), now=_MARKED_AT) is True
    # A reduction is allowed even when the mark later becomes stale.
    stale_now = _MARKED_AT + timedelta(seconds=6)
    assert risk.check(_request(OrderSide.SELL), now=stale_now) is True


def test_live_risk_rejects_new_exposure_when_instrument_quote_is_missing() -> None:
    positions: list[Position] = []
    risk = RiskManager(
        require_fresh_marks=True,
        positions_provider=lambda: positions,
        price_provider=lambda _instrument: None,
    )
    assert risk.check(_request(), now=_MARKED_AT) is False
