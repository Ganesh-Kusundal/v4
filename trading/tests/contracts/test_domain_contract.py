"""Domain contract tests — ported from v3 test_domain_contract.py.

Tests the canonical domain layer: InstrumentId, instrument hierarchy,
Quote/Depth/OHLC/Candle/HistoricalSeries, OptionChain, Order lifecycle,
Portfolio objects, error model, serialization, and strategy objects.
"""

from __future__ import annotations

from datetime import UTC, date, datetime
from decimal import Decimal

import pytest
from tradex_domain import (
    OHLC,
    AccountId,
    Candle,
    CapabilityNotSupportedError,
    Condition,
    Depth,
    Equity,
    Expiry,
    Future,
    HistoricalSeries,
    Index,
    InstrumentId,
    Money,
    Option,
    OptionChain,
    OptionPair,
    Order,
    OrderId,
    OrderRejectedError,
    OrderRequest,
    OrderSide,
    OrderStatus,
    OrderType,
    PortfolioSnapshot,
    Position,
    Price,
    ProductType,
    Quantity,
    Quote,
    ScannerResult,
    SDKError,
    Timeframe,
    TimeInForce,
)
from tradex_domain.errors import (
    AuthenticationError,
    BrokerUnavailableError,
    InstrumentNotFoundError,
    RateLimitError,
    SessionStateError,
)


def _now() -> datetime:
    return datetime(2026, 7, 31, 10, 30, tzinfo=UTC)


# ---------------------------------------------------------------------------
# F1 — InstrumentId
# ---------------------------------------------------------------------------


def test_instrument_id_canonical_serialization() -> None:
    eq = InstrumentId.equity("NSE", " reliance ")
    assert eq.exchange == "NSE"
    assert eq.underlying == "RELIANCE"
    assert InstrumentId.parse("NFO:NIFTY:20260730:25000:CE") == InstrumentId.option(
        "NFO", "NIFTY", date(2026, 7, 30), Decimal("25000"), "CE"
    )
    assert str(InstrumentId.future("NFO", "NIFTY", date(2026, 7, 30))) == "NFO:NIFTY:20260730:FUT"


def test_instrument_id_equality_and_hash() -> None:
    a = InstrumentId.equity("NSE", "RELIANCE")
    b = InstrumentId.equity("nse", "reliance")
    assert a == b
    assert hash(a) == hash(b)


# ---------------------------------------------------------------------------
# F2 — Instrument hierarchy
# ---------------------------------------------------------------------------


def test_instrument_hierarchy_types() -> None:
    eq = Equity.of("NSE", "RELIANCE")
    idx = Index.of("NSE", "NIFTY")
    fut = Future.of("NFO", "NIFTY", date(2026, 7, 30))
    opt = Option.of("NFO", "NIFTY", date(2026, 7, 30), Decimal("25000"), "CE")
    assert eq.asset_class.value == "EQUITY"
    assert idx.asset_class.value == "INDEX"
    assert fut.expiry == date(2026, 7, 30)
    assert opt.right == "CE"
    assert opt.strike == Decimal("25000")


def test_option_validates_right() -> None:
    with pytest.raises(ValueError):
        Option.of("NFO", "NIFTY", date(2026, 7, 30), Decimal("25000"), "XX")


# ---------------------------------------------------------------------------
# F3 — Quote / Depth / OHLC / Candle / HistoricalSeries
# ---------------------------------------------------------------------------


def test_quote_has_full_fds_shape() -> None:
    eq = Equity.of("NSE", "RELIANCE")
    quote = Quote(
        instrument=eq,
        ltp=Price(value=Decimal("2490.50")),
        bid=Price(value=Decimal("2490.00")),
        ask=Price(value=Decimal("2491.00")),
        volume=Quantity(value=Decimal("100")),
        timestamp=_now(),
        exchange="NSE",
        provider="stub",
    )
    assert quote.instrument == eq
    assert quote.ltp == Price(value=Decimal("2490.50"))


def test_depth_rename_ratified() -> None:
    assert Depth.__name__ == "Depth"


def test_historical_series_window() -> None:
    eq = Equity.of("NSE", "RELIANCE")
    candles = [
        Candle(
            instrument=eq,
            timeframe=Timeframe.M1,
            ohlc=OHLC(
                open=Price(value=Decimal("10")),
                high=Price(value=Decimal("12")),
                low=Price(value=Decimal("9")),
                close=Price(value=Decimal("11")),
            ),
            volume=Quantity(value=Decimal("100")),
            timestamp=_now(),
        )
        for _ in range(5)
    ]
    series = HistoricalSeries(
        instrument=eq,
        timeframe=Timeframe.M1,
        candles=candles,
        start=_now(),
        end=_now(),
    )
    assert series.window(3).candles == candles[-3:]


# ---------------------------------------------------------------------------
# F4 — OptionChain / Expiry / OptionPair
# ---------------------------------------------------------------------------


def _chain() -> tuple[OptionChain, Index]:
    nifty = Index.of("NSE", "NIFTY")
    expiry = date(2026, 7, 30)
    pairs = tuple(
        OptionPair(
            call=Option.of("NFO", "NIFTY", expiry, Decimal(str(s)), "CE"),
            put=Option.of("NFO", "NIFTY", expiry, Decimal(str(s)), "PE"),
            strike=Price(value=Decimal(str(s))),
        )
        for s in (24800, 24900, 25000, 25100, 25200)
    )
    exp = Expiry(
        underlying=nifty,
        expiry_date=expiry,
        pairs=pairs,
        reference_price=Price(value=Decimal("25000")),
    )
    return OptionChain(underlying=nifty, _expiries=(exp,)), nifty


def test_option_chain_expiries_method() -> None:
    chain, _ = _chain()
    assert [e.expiry_date for e in chain.expiries()] == [date(2026, 7, 30)]
    assert chain.expiry(date(2026, 7, 30)) is not None
    assert chain.expiry(date(2027, 1, 1)) is None


def test_expiry_atm_returns_pair() -> None:
    chain, _ = _chain()
    exp = chain.expiries()[0]
    pair = exp.atm(0)
    assert isinstance(pair, OptionPair)
    assert pair.strike == Price(value=Decimal("25000"))


def test_expiry_otm_and_itm_are_distinct() -> None:
    chain, _ = _chain()
    exp = chain.expiries()[0]
    otm_strikes = [p.strike.value for p in exp.otm(2)]
    itm_strikes = [p.strike.value for p in exp.itm(2)]
    assert set(otm_strikes).isdisjoint(set(itm_strikes))
    assert otm_strikes == [Decimal("25100"), Decimal("25200")]
    assert itm_strikes == [Decimal("24900"), Decimal("24800")]


def test_depth_serialization_round_trip() -> None:
    eq = Equity.of("NSE", "RELIANCE")
    depth = Depth(
        instrument=eq,
        bids=((Price(value=Decimal("10")), Quantity(value=Decimal("2"))),),
        asks=(),
        timestamp=_now(),
    )
    assert Depth.from_dict(depth.to_dict()) == depth


def test_order_serialization_round_trip() -> None:
    order = _order().transition_to(OrderStatus.PENDING)
    assert Order.from_dict(order.to_dict()) == order


def test_order_historical_positional_constructor_preserves_filled_quantity() -> None:
    order = Order(
        OrderId(value="positional-1"),
        Equity.of("NSE", "RELIANCE"),
        OrderSide.BUY,
        OrderType.MARKET,
        Quantity(value=Decimal("10")),
        None,
        TimeInForce.DAY,
        OrderStatus.FILLED,
        None,
        None,
        ProductType.INTRADAY,
        None,
        Quantity(value=Decimal("10")),
    )
    assert order.filled_quantity == Quantity(value=Decimal("10"))
    assert order.target_price is None
    assert order.stop_loss_price is None
    assert order.trailing_jump is None


def test_order_extension_fields_survive_serialization_and_transition() -> None:
    request = OrderRequest(
        instrument=Equity.of("NSE", "RELIANCE"),
        side=OrderSide.BUY,
        order_type=OrderType.LIMIT,
        quantity=Quantity(value=Decimal("10")),
        price=Price(value=Decimal("100")),
        target_price=Price(value=Decimal("110")),
        stop_loss_price=Price(value=Decimal("95")),
        trailing_jump=Price(value=Decimal("2")),
    )
    restored_request = OrderRequest.from_dict(request.to_dict())
    assert restored_request == request
    order = Order(
        order_id=OrderId(value="super-1"),
        instrument=request.instrument,
        side=request.side,
        order_type=request.order_type,
        quantity=request.quantity,
        price=request.price,
        time_in_force=request.time_in_force,
        status=OrderStatus.PENDING,
        target_price=request.target_price,
        stop_loss_price=request.stop_loss_price,
        trailing_jump=request.trailing_jump,
    )
    assert Order.from_dict(order.to_dict()) == order
    transitioned = order.transition_to(OrderStatus.ACK)
    assert transitioned.target_price == request.target_price
    assert transitioned.stop_loss_price == request.stop_loss_price
    assert transitioned.trailing_jump == request.trailing_jump


# ---------------------------------------------------------------------------
# F5 — Order lifecycle state machine
# ---------------------------------------------------------------------------


def _order() -> Order:
    return Order(
        order_id=OrderId(value="ord-1"),
        instrument=Equity.of("NSE", "RELIANCE"),
        side=OrderSide.BUY,
        order_type=OrderType.MARKET,
        quantity=Quantity(value=Decimal("10")),
        price=None,
        time_in_force=TimeInForce.DAY,
        status=OrderStatus.NEW,
    )


def test_order_lifecycle_transitions() -> None:
    order = _order()
    order = order.transition_to(OrderStatus.PENDING)
    assert order.status == OrderStatus.PENDING
    order = order.transition_to(OrderStatus.ACK)
    order = order.transition_to(OrderStatus.FILLED)
    assert order.status == OrderStatus.FILLED


def test_order_rejects_illegal_transition() -> None:
    order = _order()
    with pytest.raises(OrderRejectedError):
        order.transition_to(OrderStatus.FILLED)


def test_order_request_is_typed() -> None:
    req = OrderRequest(
        instrument=Equity.of("NSE", "RELIANCE"),
        side=OrderSide.BUY,
        order_type=OrderType.LIMIT,
        quantity=Quantity(value=Decimal("5")),
        price=Price(value=Decimal("100")),
    )
    assert req.instrument.underlying == "RELIANCE"


# ---------------------------------------------------------------------------
# F6 — Portfolio objects
# ---------------------------------------------------------------------------


def test_portfolio_snapshot() -> None:
    eq = Equity.of("NSE", "RELIANCE")
    pos = Position(
        instrument=eq,
        quantity=Quantity(value=Decimal("10")),
        avg_price=Price(value=Decimal("2480")),
        realized_pnl=Money(amount=Decimal("0"), currency="INR"),
        unrealized_pnl=Money(amount=Decimal("120"), currency="INR"),
    )
    snap = PortfolioSnapshot(positions=[pos])
    assert snap.positions[0].instrument == eq


def test_account_snapshot_typed() -> None:
    from tradex_domain import Account

    snap = Account(
        account_id=AccountId(value="ACC-1"), balance=Money(amount=Decimal("500000"))
    )
    assert snap.balance == Money(amount=Decimal("500000"))


# ---------------------------------------------------------------------------
# N1 — Error model
# ---------------------------------------------------------------------------


def test_error_hierarchy() -> None:
    for exc in (
        AuthenticationError,
        RateLimitError,
        BrokerUnavailableError,
        OrderRejectedError,
        InstrumentNotFoundError,
        CapabilityNotSupportedError,
        SessionStateError,
    ):
        assert issubclass(exc, SDKError)


# ---------------------------------------------------------------------------
# N12 — Serialization round-trip
# ---------------------------------------------------------------------------


def test_value_object_serialization_round_trip() -> None:
    pid = InstrumentId.option("NFO", "NIFTY", date(2026, 7, 30), Decimal("25000"), "CE")
    assert InstrumentId.from_dict(pid.to_dict()) == pid
    price = Price(value=Decimal("2490.50"))
    assert Price.from_dict(price.to_dict()) == price


def test_quote_serialization_round_trip() -> None:
    eq = Equity.of("NSE", "RELIANCE")
    quote = Quote(
        instrument=eq,
        ltp=Price(value=Decimal("2490.50")),
        bid=Price(value=Decimal("2490.00")),
        ask=Price(value=Decimal("2491.00")),
        volume=Quantity(value=Decimal("100")),
        timestamp=_now(),
        exchange="NSE",
        provider="stub",
    )
    restored = Quote.from_dict(quote.to_dict())
    assert restored == quote


# ---------------------------------------------------------------------------
# N13 — Strategy/scanner object baseline
# ---------------------------------------------------------------------------


def test_scanner_result_shape() -> None:
    eq = Equity.of("NSE", "RELIANCE")
    result = ScannerResult(
        instrument=eq,
        score=1.0,
        matched_conditions=["rsi_above"],
        indicator_values={"rsi": 70.0},
        rank=1,
    )
    assert result.indicator_values["rsi"] == 70.0


def test_condition_no_any() -> None:
    cond = Condition(name="rsi_above", operator=">", threshold=70)
    assert cond.threshold == 70
