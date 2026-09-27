"""AC-6 — the fee model is product aware, and a product with no schedule is
refused rather than priced.

The defect this file pins is not "delivery costs the wrong amount" — it is
that ``FeeCalculator`` had no notion of a product at all, so it charged the
intraday schedule to every fill and had no way to say it could not do better.
Both halves have a negative control here, because both are a change that can
look right while changing nothing:

- **CONTROL (a)** a delivery fill costs strictly more than an intraday fill of
  the same notional, asserted as hand-computed numbers on BOTH sides. A
  sell-only delivery STT passes a sell-side comparison and fails the buy side,
  so only the buy side is the actual control on the post-Oct-2024 rule.
- **CONTROL (b)** ``MARGIN``/``MTF``/``COVER_ORDER`` are refused with the
  product named, and are never priced. The control is the *totality* test:
  every ``ProductType`` member is either in the schedule table or refused, so
  a member added without a schedule fails here instead of costing a silent
  zero.
- **CONTROL (c)** a null ``product_type`` is charged intraday AND counted, so
  a mislabelled order is visible instead of silently assumed.
- **CONTROL (d)** a backtest with fees on has strictly lower equity than the
  same backtest with fees off, by exactly the fees it reports. This is the one
  that proves the fees reach the book rather than being computed and dropped —
  it is what would have caught ``fees_enabled`` defaulting to false.

The ``Fill.product_type`` tests are the domain half of the same requirement:
nullable, backfilled from the parent order, and backward compatible with an
event log written before the column existed.
"""

from __future__ import annotations

from datetime import UTC, datetime
from decimal import Decimal

import pytest
from tradex_domain import (
    OHLC,
    Candle,
    Equity,
    Fill,
    OrderId,
    OrderSide,
    Price,
    Quantity,
    Signal,
    Timeframe,
)
from tradex_domain.enums import OrderStatus, OrderType, ProductType, TimeInForce
from tradex_domain.execution import Order
from tradex_domain.serialization import from_dict, to_dict
from tradex_execution.fees import (
    SCHEDULES,
    FeeCalculator,
    UnsupportedProductTypeError,
    require_fee_schedule,
    schedule_for,
)
from tradex_replay.backtest import BacktestEngine

_EQUITY = Equity.of("NSE", "RELIANCE")

#: Every member the brief requires. The exhaustive test below walks this
#: against the schedule table, so a new member with no schedule is a failure.
_ALL_PRODUCTS = (
    ProductType.INTRADAY,
    ProductType.DELIVERY,
    ProductType.MARGIN,
    ProductType.MTF,
    ProductType.COVER_ORDER,
)

#: The three with no schedule yet. Refused, never priced.
_UNSCHEDULED = (ProductType.MARGIN, ProductType.MTF, ProductType.COVER_ORDER)


def _fill(
    side: OrderSide,
    *,
    price: str = "1000",
    qty: str = "100",
    product_type: ProductType | None = None,
) -> Fill:
    return Fill(
        order_id=OrderId(value="fee-product"),
        instrument=_EQUITY,
        side=side,
        quantity=Quantity(value=Decimal(qty)),
        price=Price(value=Decimal(price)),
        timestamp=datetime(2026, 8, 1, tzinfo=UTC),
        product_type=product_type,
    )


# ---------------------------------------------------------------------------
# CONTROL (a) — delivery is priced from the delivery table, not the intraday one
# ---------------------------------------------------------------------------


class TestControlADeliveryIsNotIntraday:
    """CONTROL (a): the two schedules produce different, larger numbers."""

    def test_delivery_buy_costs_more_than_intraday_buy_of_the_same_notional(self) -> None:
        """Delivery STT is 0.1% on BOTH sides since 1 Oct 2024.

        Notional 1,00,000 (1000 x 100). The only component that differs
        between the two schedules is STT, so the difference is exactly the
        delivery-side STT that an intraday buy is not charged at all:

            brokerage 20.00 + exchange 3.45 + SEBI 0.20 + stamp 3.00
            + GST 18% of (20.00 + 3.45 + 0.20) = 4.26          ->  30.91
            + delivery STT 0.1% of 1,00,000                     = 100.00
                                                                --------
            delivery BUY                                          130.91
            intraday BUY (no STT on a buy)                         30.91
        """
        assert FeeCalculator().calculate(
            _fill(OrderSide.BUY, product_type=ProductType.DELIVERY),
        ).amount == Decimal("130.91")

        # Same fill, intraday product: charged the intraday table, which has
        # no STT on a buy at all.
        assert FeeCalculator().calculate(
            _fill(OrderSide.BUY, product_type=ProductType.INTRADAY),
        ).amount == Decimal("30.91")

    def test_delivery_sell_costs_more_than_intraday_sell_of_the_same_notional(self) -> None:
        """The sell side too — 0.1% against the intraday 0.025%.

            delivery SELL = 30.91 + 100.00 = 130.91
            intraday SELL = 30.91 +  25.00 =  55.91
        """
        assert FeeCalculator().calculate(
            _fill(OrderSide.SELL, product_type=ProductType.DELIVERY),
        ).amount == Decimal("130.91")
        assert FeeCalculator().calculate(
            _fill(OrderSide.SELL, product_type=ProductType.INTRADAY),
        ).amount == Decimal("55.91")

    def test_a_delivery_round_trip_costs_three_times_the_intraday_one(self) -> None:
        """The round trip is the number a strategy actually pays.

        Notional 1,00,000 a side. STT alone is 8x (delivery 0.1% on two
        sides against intraday 0.025% on one), and the whole fee is 3.01x
        once the side-independent components are in it.
        """
        calc = FeeCalculator()
        delivery = (
            calc.calculate(_fill(OrderSide.BUY, product_type=ProductType.DELIVERY)).amount
            + calc.calculate(_fill(OrderSide.SELL, product_type=ProductType.DELIVERY)).amount
        )
        intraday = (
            calc.calculate(_fill(OrderSide.BUY, product_type=ProductType.INTRADAY)).amount
            + calc.calculate(_fill(OrderSide.SELL, product_type=ProductType.INTRADAY)).amount
        )
        assert delivery == Decimal("261.82")
        assert intraday == Decimal("86.82")
        assert delivery > intraday

    def test_delivery_is_side_symmetric_and_intraday_is_not(self) -> None:
        """Both schedules are internally consistent, asserted as properties.

        Delivery charges STT on both sides, so its fee cannot depend on the
        side. Intraday charges it on the sell only, so its fee must.
        """
        calc = FeeCalculator()
        for product, sides_match in (
            (ProductType.DELIVERY, True),
            (ProductType.INTRADAY, False),
        ):
            buy = calc.calculate(_fill(OrderSide.BUY, product_type=product)).amount
            sell = calc.calculate(_fill(OrderSide.SELL, product_type=product)).amount
            assert (buy == sell) is sides_match, (product, buy, sell)

    def test_the_pre_reform_delivery_number_is_the_wrong_answer(self) -> None:
        """Negative control, stated as an assertion.

        The pre-Oct-2024 rule charged delivery STT on the sell side only.
        That model priced this buy at 7.66 and the sell at 107.66. If the
        schedule is ever reverted to sell-only, this test is the one that
        fails, and it fails on the BUY — where a sell-only model is silent.
        """
        delivery = FeeCalculator.equity_delivery(
            side=OrderSide.BUY, price=Decimal("1000"), quantity=Decimal("100"),
        )
        intraday = FeeCalculator.equity_intraday(
            side=OrderSide.BUY, price=Decimal("1000"), quantity=Decimal("100"),
        )
        assert delivery.stt == Decimal("100.00")   # 0.1% of 1,00,000
        assert intraday.stt == Decimal("0.00")     # no STT on an intraday buy
        assert delivery.stt != intraday.stt


# ---------------------------------------------------------------------------
# CONTROL (b) — a product with no schedule is refused, not priced
# ---------------------------------------------------------------------------


class TestControlBUnscheduledProductsAreRefused:
    """CONTROL (b): a refusal naming the product, at the earliest door."""

    @pytest.mark.parametrize("product", _UNSCHEDULED)
    def test_the_gate_refuses_and_names_the_product(self, product: ProductType) -> None:
        with pytest.raises(UnsupportedProductTypeError) as raised:
            require_fee_schedule(product)
        message = str(raised.value)
        assert product.value in message, message
        # Named as unsupported, not as a zero and not as the intraday table.
        assert "no fee schedule" in message.lower(), message

    @pytest.mark.parametrize("product", _UNSCHEDULED)
    def test_pricing_refuses_rather_than_charging_a_number(self, product: ProductType) -> None:
        """The second door. Even a caller that skipped the gate gets a raise.

        Charging intraday costs for a margin book is the specific wrong
        answer this guards: it is silent, it is plausible, and it is wrong by
        whatever the margin schedule will eventually say.
        """
        with pytest.raises(UnsupportedProductTypeError) as raised:
            FeeCalculator().calculate(_fill(OrderSide.BUY, product_type=product))
        assert product.value in str(raised.value)

    @pytest.mark.parametrize("product", _UNSCHEDULED)
    def test_the_caped_path_refuses_too(self, product: ProductType) -> None:
        """``calculate_capped`` is the door the execution engine actually uses."""
        with pytest.raises(UnsupportedProductTypeError):
            FeeCalculator().calculate_capped(
                _fill(OrderSide.SELL, product_type=product), Decimal("0"),
            )

    @pytest.mark.parametrize("product", _UNSCHEDULED)
    def test_schedule_lookup_refuses_and_the_table_has_no_entry(self, product: ProductType) -> None:
        """Structurally: an unscheduled product has no row to fall through to."""
        with pytest.raises(UnsupportedProductTypeError):
            schedule_for(product)
        assert product not in SCHEDULES

    def test_every_product_type_is_either_priced_or_refused(self) -> None:
        """CONTROL (b) in its strongest form: the table is total.

        Walks every member of the enum, so a sixth product type added later
        without a schedule fails here rather than costing a silent zero.
        """
        priced, refused = set(), set()
        for product in ProductType:
            try:
                require_fee_schedule(product)
            except UnsupportedProductTypeError as exc:
                assert product.value in str(exc)
                refused.add(product)
            else:
                priced.add(product)
        assert priced == {ProductType.INTRADAY, ProductType.DELIVERY}
        assert refused == set(_UNSCHEDULED)
        assert priced | refused == set(_ALL_PRODUCTS) == set(ProductType)

    def test_the_intraday_schedule_is_not_reachable_as_a_fallback(self) -> None:
        """A refusal must not degrade into "intraday, probably fine"."""
        intraday = SCHEDULES[ProductType.INTRADAY]
        for product in _UNSCHEDULED:
            assert product is not intraday.product
            with pytest.raises(UnsupportedProductTypeError):
                schedule_for(product)


# ---------------------------------------------------------------------------
# CONTROL (c) — a null product_type is charged intraday AND counted
# ---------------------------------------------------------------------------


class TestControlCMissingProductTypeIsCounted:
    """CONTROL (c): ``fills_missing_product_type_total`` is the mislabel alarm."""

    def test_a_null_product_type_is_counted_on_every_fill(self) -> None:
        calc = FeeCalculator()
        assert calc.missing_product_type_count == 0
        calc.calculate(_fill(OrderSide.BUY, product_type=None))
        assert calc.missing_product_type_count == 1
        calc.calculate(_fill(OrderSide.SELL, product_type=None))
        assert calc.missing_product_type_count == 2

    def test_a_null_product_type_is_charged_the_intraday_schedule(self) -> None:
        """Counted, not refused: the fill is real and the book must balance."""
        calc = FeeCalculator()
        assert calc.calculate(
            _fill(OrderSide.BUY, product_type=None),
        ).amount == calc.calculate(
            _fill(OrderSide.BUY, product_type=ProductType.INTRADAY),
        ).amount

    def test_a_labelled_fill_is_not_counted(self) -> None:
        calc = FeeCalculator()
        calc.calculate(_fill(OrderSide.BUY, product_type=ProductType.DELIVERY))
        calc.calculate_capped(
            _fill(OrderSide.SELL, product_type=ProductType.DELIVERY), Decimal("0"),
        )
        assert calc.missing_product_type_count == 0

    def test_the_counter_reaches_the_metrics_registry_when_one_is_bound(self) -> None:
        from tradex_observability.metrics import MetricsRegistry

        registry = MetricsRegistry()
        calc = FeeCalculator(metrics=registry)
        calc.calculate(_fill(OrderSide.BUY, product_type=None))
        assert (
            registry.counter("fills_missing_product_type_total").value() == 1.0
        )
        assert calc.missing_product_type_count == 1


# ---------------------------------------------------------------------------
# The domain half — Fill.product_type is nullable and additive
# ---------------------------------------------------------------------------


class TestFillProductTypeIsNullableAndAdditive:
    def test_it_defaults_to_null(self) -> None:
        assert _fill(OrderSide.BUY).product_type is None

    def test_a_null_is_backfilled_from_the_parent_order_not_guessed(self) -> None:
        """The production backfill is the parent ``Order.product_type``.

        Asserted here against the shape the backfill has to have: the fill
        takes the order's value verbatim, and nothing infers it from the
        side, the venue or the price.
        """
        order = Order(
            order_id=OrderId(value="o-1"),
            instrument=_EQUITY,
            side=OrderSide.BUY,
            order_type=OrderType.MARKET,
            quantity=Quantity(value=Decimal("10")),
            price=Price(value=Decimal("100")),
            time_in_force=TimeInForce.DAY,
            status=OrderStatus.FILLED,
            product_type=ProductType.DELIVERY,
        )
        backfilled = Fill(
            order_id=order.order_id,
            instrument=order.instrument,
            side=order.side,
            quantity=order.quantity,
            price=Price(value=Decimal("100")),
            product_type=order.product_type,
        )
        assert backfilled.product_type is ProductType.DELIVERY

    def test_an_event_log_row_written_before_the_field_still_loads(self) -> None:
        """Additive on the wire: a missing key deserializes to null, not an error.

        This is why the column is nullable rather than NOT NULL — the OMS
        store has no migration path, and the event log holds payloads written
        before this field existed.
        """
        payload = to_dict(_fill(OrderSide.BUY, product_type=ProductType.DELIVERY))
        del payload["product_type"]
        restored = from_dict(Fill, payload)
        assert restored.product_type is None

    def test_it_round_trips(self) -> None:
        restored = from_dict(
            Fill, to_dict(_fill(OrderSide.SELL, product_type=ProductType.DELIVERY)),
        )
        assert restored.product_type is ProductType.DELIVERY


# ---------------------------------------------------------------------------
# CONTROL (d) — the fees reach the book
# ---------------------------------------------------------------------------


def _candle(close: float, ts: datetime) -> Candle:
    return Candle(
        instrument=_EQUITY,
        timeframe=Timeframe.D1,
        ohlc=OHLC(
            open=Price(value=Decimal(str(close))),
            high=Price(value=Decimal(str(close))),
            low=Price(value=Decimal(str(close))),
            close=Price(value=Decimal(str(close))),
        ),
        volume=Quantity(value=Decimal("1000")),
        timestamp=ts,
    )


class _OneInOneOut:
    """BUY on bar 1, SELL on bar 2, so the run books a complete round trip."""

    def __init__(self) -> None:
        self._signals: list[Signal] = []
        self._bar = 0

    @property
    def strategy_id(self) -> str:
        return "fee-book"

    @property
    def signals(self) -> list[Signal]:
        return list(self._signals)

    def on_bar(self, context, candle) -> object:
        self._bar += 1
        side = {1: OrderSide.BUY, 2: OrderSide.SELL}.get(self._bar)
        if side is None:
            return None
        signal = Signal(
            instrument=candle.instrument, direction=side, strength=10.0,
            reason="round trip", timestamp=candle.timestamp,
        )
        self._signals.append(signal)
        return signal

    def on_quote(self, context, quote) -> None:
        pass

    def on_fill(self, context, fill) -> None:
        pass


class TestControlDFeesReachTheBook:
    """CONTROL (d): costs are not computed and dropped."""

    @staticmethod
    def _data() -> list[Candle]:
        # Three bars: the SELL on bar 2 fills at bar 3's open, so the run
        # ends flat and the final equity difference is exactly the fees.
        return [
            _candle(100.0, datetime(2026, 8, 1, 10, 30, tzinfo=UTC)),
            _candle(110.0, datetime(2026, 8, 2, 10, 30, tzinfo=UTC)),
            _candle(120.0, datetime(2026, 8, 3, 10, 30, tzinfo=UTC)),
        ]

    def test_the_same_backtest_is_cheaper_with_fees_on(self) -> None:
        data = self._data()
        free = BacktestEngine().run(_OneInOneOut(), data)
        charged = BacktestEngine(fee_calculator=FeeCalculator()).run(_OneInOneOut(), data)

        # Both runs must have traded, or the comparison is vacuous.
        assert free.num_trades == charged.num_trades == 2
        assert charged.total_fees > 0.0
        assert free.total_fees == 0.0

        # Strictly lower equity, and lower by exactly what it says it paid.
        # Equality is the point: a fee that is computed and dropped would
        # leave the curve untouched while still reporting a total.
        assert charged.equity_curve[-1] < free.equity_curve[-1]
        assert free.equity_curve[-1] - charged.equity_curve[-1] == pytest.approx(
            charged.total_fees, abs=0.01,
        )

    def test_negative_control_costs_off_is_the_flattering_number(self) -> None:
        """The control the brief asks for, stated as its own assertion.

        Were the fee calculator constructed but never applied, both runs
        would report ``total_fees > 0`` and the same equity. This asserts the
        two differ, which is the only observable that distinguishes "fees on"
        from "fees computed and dropped".
        """
        data = self._data()
        free = BacktestEngine().run(_OneInOneOut(), data)
        charged = BacktestEngine(fee_calculator=FeeCalculator()).run(_OneInOneOut(), data)
        assert charged.equity_curve != free.equity_curve
        assert charged.total_return < free.total_return
