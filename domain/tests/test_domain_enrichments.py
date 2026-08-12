"""Tests for Order predicate properties and Position PnL helpers."""

from __future__ import annotations

from datetime import UTC, datetime
from decimal import Decimal

import pytest

from tradex_domain.enums import (
    OrderSide,
    OrderStatus,
    OrderType,
    ProductType,
    Timeframe,
    TimeInForce,
)
from tradex_domain.execution import Account, Fill, Order, PortfolioSnapshot, Position
from tradex_domain.instruments import Equity
from tradex_domain.market import OHLC, Candle, HistoricalSeries
from tradex_domain.strategy import Signal
from tradex_domain.value_objects import AccountId, Money, OrderId, Price, Quantity

_NSE_RELIANCE = Equity.of("NSE", "RELIANCE")


# ---------------------------------------------------------------------------
# Order predicate helpers
# ---------------------------------------------------------------------------


def _make_order(status: OrderStatus) -> Order:
    return Order(
        order_id=OrderId("ORD-1"),
        instrument=_NSE_RELIANCE,
        side=OrderSide.BUY,
        order_type=OrderType.LIMIT,
        quantity=Quantity(Decimal("10")),
        price=Price(Decimal("100")),
        time_in_force=TimeInForce.DAY,
        status=status,
        product_type=ProductType.INTRADAY,
    )


class TestOrderIsActive:
    @pytest.mark.parametrize(
        "status",
        [
            OrderStatus.NEW,
            OrderStatus.PENDING,
            OrderStatus.ACK,
            OrderStatus.PARTIALLY_FILLED,
            OrderStatus.SUBMITTED,
        ],
    )
    def test_active_statuses(self, status: OrderStatus):
        assert _make_order(status).is_active is True

    @pytest.mark.parametrize(
        "status",
        [
            OrderStatus.FILLED,
            OrderStatus.CANCELLED,
            OrderStatus.REJECTED,
            OrderStatus.UNKNOWN,
        ],
    )
    def test_non_active_statuses(self, status: OrderStatus):
        assert _make_order(status).is_active is False


class TestOrderIsFilled:
    def test_filled(self):
        assert _make_order(OrderStatus.FILLED).is_filled is True

    @pytest.mark.parametrize(
        "status",
        [s for s in OrderStatus if s != OrderStatus.FILLED],
    )
    def test_non_filled(self, status: OrderStatus):
        assert _make_order(status).is_filled is False


class TestOrderIsCancelled:
    def test_cancelled(self):
        assert _make_order(OrderStatus.CANCELLED).is_cancelled is True

    @pytest.mark.parametrize(
        "status",
        [s for s in OrderStatus if s != OrderStatus.CANCELLED],
    )
    def test_non_cancelled(self, status: OrderStatus):
        assert _make_order(status).is_cancelled is False


class TestOrderIsPending:
    @pytest.mark.parametrize(
        "status",
        [OrderStatus.NEW, OrderStatus.PENDING, OrderStatus.SUBMITTED],
    )
    def test_pending_statuses(self, status: OrderStatus):
        assert _make_order(status).is_pending is True

    @pytest.mark.parametrize(
        "status",
        [
            OrderStatus.ACK,
            OrderStatus.PARTIALLY_FILLED,
            OrderStatus.FILLED,
            OrderStatus.CANCELLED,
            OrderStatus.REJECTED,
            OrderStatus.UNKNOWN,
        ],
    )
    def test_non_pending_statuses(self, status: OrderStatus):
        assert _make_order(status).is_pending is False


# ---------------------------------------------------------------------------
# Position PnL helpers
# ---------------------------------------------------------------------------


def _make_position(
    qty: str = "10",
    avg_price: str = "100",
    realized: str = "0",
    unrealized: str = "0",
    currency: str = "INR",
) -> Position:
    return Position(
        instrument=_NSE_RELIANCE,
        quantity=Quantity(Decimal(qty)),
        avg_price=Price(Decimal(avg_price)),
        realized_pnl=Money(amount=Decimal(realized), currency=currency),
        unrealized_pnl=Money(amount=Decimal(unrealized), currency=currency),
    )


class TestPositionTotalPnl:
    def test_both_positive(self):
        pos = _make_position(realized="500", unrealized="300")
        assert pos.total_pnl.amount == Decimal("800")
        assert pos.total_pnl.currency == "INR"

    def test_realized_only(self):
        pos = _make_position(realized="1200", unrealized="0")
        assert pos.total_pnl.amount == Decimal("1200")

    def test_unrealized_only(self):
        pos = _make_position(realized="0", unrealized="-400")
        assert pos.total_pnl.amount == Decimal("-400")

    def test_netting(self):
        pos = _make_position(realized="1000", unrealized="-250")
        assert pos.total_pnl.amount == Decimal("750")

    def test_preserves_currency(self):
        pos = _make_position(realized="10", unrealized="5", currency="USD")
        assert pos.total_pnl.currency == "USD"


class TestPositionDirection:
    def test_long(self):
        pos = _make_position(qty="10")
        assert pos.is_long is True
        assert pos.is_short is False

    def test_short(self):
        pos = _make_position(qty="-5")
        assert pos.is_long is False
        assert pos.is_short is True

    def test_zero_is_neither(self):
        pos = _make_position(qty="0")
        assert pos.is_long is False
        assert pos.is_short is False


class TestPositionMarketValue:
    def test_basic(self):
        pos = _make_position(qty="10", avg_price="150")
        assert pos.market_value == Money(amount=Decimal("1500"), currency="INR")

    def test_fractional(self):
        pos = _make_position(qty="2.5", avg_price="40.50")
        assert pos.market_value == Money(amount=Decimal("101.250"), currency="INR")

    def test_zero_quantity(self):
        pos = _make_position(qty="0", avg_price="200")
        assert pos.market_value == Money(amount=Decimal("0"), currency="INR")


# ---------------------------------------------------------------------------
# Task 2.6: Order new predicates
# ---------------------------------------------------------------------------


class TestOrderIsTerminal:
    @pytest.mark.parametrize(
        "status",
        [OrderStatus.FILLED, OrderStatus.CANCELLED, OrderStatus.REJECTED],
    )
    def test_terminal_statuses(self, status: OrderStatus):
        assert _make_order(status).is_terminal is True

    @pytest.mark.parametrize(
        "status",
        [
            OrderStatus.NEW,
            OrderStatus.PENDING,
            OrderStatus.ACK,
            OrderStatus.PARTIALLY_FILLED,
            OrderStatus.SUBMITTED,
            OrderStatus.UNKNOWN,
        ],
    )
    def test_non_terminal_statuses(self, status: OrderStatus):
        assert _make_order(status).is_terminal is False


class TestOrderIsRejected:
    def test_rejected(self):
        assert _make_order(OrderStatus.REJECTED).is_rejected is True

    @pytest.mark.parametrize(
        "status",
        [s for s in OrderStatus if s != OrderStatus.REJECTED],
    )
    def test_non_rejected(self, status: OrderStatus):
        assert _make_order(status).is_rejected is False


class TestOrderIsPartiallyFilled:
    def test_partially_filled(self):
        assert _make_order(OrderStatus.PARTIALLY_FILLED).is_partially_filled is True

    @pytest.mark.parametrize(
        "status",
        [s for s in OrderStatus if s != OrderStatus.PARTIALLY_FILLED],
    )
    def test_non_partially_filled(self, status: OrderStatus):
        assert _make_order(status).is_partially_filled is False


class TestOrderRemainingQuantity:
    def test_no_fills(self):
        order = _make_order(OrderStatus.NEW)
        assert order.remaining_quantity == Decimal("10")

    def test_partial_fill(self):
        order = Order(
            order_id=OrderId("ORD-2"),
            instrument=_NSE_RELIANCE,
            side=OrderSide.BUY,
            order_type=OrderType.LIMIT,
            quantity=Quantity(Decimal("10")),
            price=Price(Decimal("100")),
            time_in_force=TimeInForce.DAY,
            status=OrderStatus.PARTIALLY_FILLED,
            filled_quantity=Quantity(Decimal("4")),
        )
        assert order.remaining_quantity == Decimal("6")

    def test_fully_filled(self):
        order = Order(
            order_id=OrderId("ORD-3"),
            instrument=_NSE_RELIANCE,
            side=OrderSide.BUY,
            order_type=OrderType.LIMIT,
            quantity=Quantity(Decimal("10")),
            price=Price(Decimal("100")),
            time_in_force=TimeInForce.DAY,
            status=OrderStatus.FILLED,
            filled_quantity=Quantity(Decimal("10")),
        )
        assert order.remaining_quantity == Decimal("0")


# ---------------------------------------------------------------------------
# Task 2.7: HistoricalSeries collection protocols
# ---------------------------------------------------------------------------


def _make_candle(timestamp: datetime, open_val: str = "100", close_val: str = "105") -> Candle:
    return Candle(
        instrument=_NSE_RELIANCE,
        timeframe=Timeframe.M1,
        ohlc=OHLC(
            open=Price(Decimal(open_val)),
            high=Price(Decimal("110")),
            low=Price(Decimal("95")),
            close=Price(Decimal(close_val)),
        ),
        volume=Quantity(Decimal("1000")),
        timestamp=timestamp,
    )


def _make_series(num_candles: int = 5) -> HistoricalSeries:
    base_time = datetime(2024, 1, 1, 9, 15, tzinfo=UTC)
    candles = [
        _make_candle(
            datetime(2024, 1, 1, 9, 15 + i, tzinfo=UTC),
            open_val=str(100 + i),
            close_val=str(105 + i),
        )
        for i in range(num_candles)
    ]
    return HistoricalSeries(
        instrument=_NSE_RELIANCE,
        timeframe=Timeframe.M1,
        candles=candles,
        start=base_time,
        end=datetime(2024, 1, 1, 9, 19, tzinfo=UTC),
    )


class TestHistoricalSeriesLen:
    def test_len(self):
        series = _make_series(5)
        assert len(series) == 5

    def test_len_empty(self):
        series = HistoricalSeries(
            instrument=_NSE_RELIANCE,
            timeframe=Timeframe.M1,
            candles=[],
            start=datetime(2024, 1, 1, tzinfo=UTC),
            end=datetime(2024, 1, 1, tzinfo=UTC),
        )
        assert len(series) == 0


class TestHistoricalSeriesIter:
    def test_iter(self):
        series = _make_series(3)
        candles = list(series)
        assert len(candles) == 3
        assert all(isinstance(c, Candle) for c in candles)


class TestHistoricalSeriesGetitem:
    def test_single_index(self):
        series = _make_series(5)
        candle = series[0]
        assert isinstance(candle, Candle)
        assert candle.ohlc.open.value == Decimal("100")

    def test_negative_index(self):
        series = _make_series(5)
        candle = series[-1]
        assert isinstance(candle, Candle)
        assert candle.ohlc.open.value == Decimal("104")

    def test_slice(self):
        series = _make_series(5)
        sliced = series[1:3]
        assert isinstance(sliced, HistoricalSeries)
        assert len(sliced) == 2
        assert sliced.instrument == _NSE_RELIANCE
        assert sliced.timeframe == Timeframe.M1


# ---------------------------------------------------------------------------
# Task 2.8: Signal, Fill, Candle, OHLC predicates
# ---------------------------------------------------------------------------


class TestSignalPredicates:
    def test_is_buy(self):
        signal = Signal(
            instrument=_NSE_RELIANCE,
            direction=OrderSide.BUY,
            strength=0.8,
            reason="test",
        )
        assert signal.direction == OrderSide.BUY

    def test_is_sell(self):
        signal = Signal(
            instrument=_NSE_RELIANCE,
            direction=OrderSide.SELL,
            strength=0.8,
            reason="test",
        )
        assert signal.direction == OrderSide.SELL


class TestFillPredicates:
    def test_is_buy(self):
        fill = Fill(
            order_id=OrderId("ORD-1"),
            instrument=_NSE_RELIANCE,
            side=OrderSide.BUY,
            quantity=Quantity(Decimal("10")),
            price=Price(Decimal("100")),
        )
        assert fill.side == OrderSide.BUY

    def test_is_sell(self):
        fill = Fill(
            order_id=OrderId("ORD-1"),
            instrument=_NSE_RELIANCE,
            side=OrderSide.SELL,
            quantity=Quantity(Decimal("10")),
            price=Price(Decimal("100")),
        )
        assert fill.side == OrderSide.SELL


class TestCandlePredicates:
    def test_is_bullish(self):
        candle = _make_candle(
            datetime(2024, 1, 1, 9, 15, tzinfo=UTC),
            open_val="100",
            close_val="105",
        )
        assert candle.is_bullish is True
        assert candle.is_bearish is False

    def test_is_bearish(self):
        candle = _make_candle(
            datetime(2024, 1, 1, 9, 15, tzinfo=UTC),
            open_val="105",
            close_val="100",
        )
        assert candle.is_bullish is False
        assert candle.is_bearish is True


class TestOHLCPredicates:
    def test_is_bullish(self):
        ohlc = OHLC(
            open=Price(Decimal("100")),
            high=Price(Decimal("110")),
            low=Price(Decimal("95")),
            close=Price(Decimal("105")),
        )
        assert ohlc.is_bullish is True

    def test_is_bearish(self):
        ohlc = OHLC(
            open=Price(Decimal("105")),
            high=Price(Decimal("110")),
            low=Price(Decimal("95")),
            close=Price(Decimal("100")),
        )
        assert ohlc.is_bullish is False

    def test_range(self):
        ohlc = OHLC(
            open=Price(Decimal("100")),
            high=Price(Decimal("115")),
            low=Price(Decimal("95")),
            close=Price(Decimal("105")),
        )
        assert ohlc.range.value == Decimal("20")


# ---------------------------------------------------------------------------
# Task 2.9: PortfolioSnapshot aggregate properties
# ---------------------------------------------------------------------------


def _make_account(equity: str = "50000") -> Account:
    return Account(
        account_id=AccountId("ACC-1"),
        balance=Money(amount=Decimal("30000"), currency="INR"),
        margin=Money(amount=Decimal("10000"), currency="INR"),
        equity=Money(amount=Decimal(equity), currency="INR"),
    )


class TestPortfolioSnapshotTotalValue:
    def test_with_account(self):
        pos = _make_position(qty="10", avg_price="100")
        account = _make_account(equity="50000")
        snapshot = PortfolioSnapshot(positions=[pos], account=account)
        assert snapshot.total_value.amount == Decimal("50000")
        assert snapshot.total_value.currency == "INR"

    def test_without_account(self):
        pos = _make_position(qty="10", avg_price="150")
        snapshot = PortfolioSnapshot(positions=[pos], account=None)
        assert snapshot.total_value.amount == Decimal("1500")

    def test_empty_positions(self):
        snapshot = PortfolioSnapshot(positions=[], account=None)
        assert snapshot.total_value.amount == Decimal("0")


class TestPortfolioSnapshotTotalPnl:
    def test_sum_of_pnl(self):
        pos1 = _make_position(realized="500", unrealized="300")
        pos2 = _make_position(realized="200", unrealized="-100")
        snapshot = PortfolioSnapshot(positions=[pos1, pos2])
        assert snapshot.total_pnl.amount == Decimal("900")

    def test_empty_positions(self):
        snapshot = PortfolioSnapshot(positions=[])
        assert snapshot.total_pnl.amount == Decimal("0")


class TestPortfolioSnapshotNetExposure:
    def test_long_positions(self):
        pos1 = _make_position(qty="10", avg_price="100")
        pos2 = _make_position(qty="5", avg_price="200")
        snapshot = PortfolioSnapshot(positions=[pos1, pos2])
        assert snapshot.net_exposure.amount == Decimal("2000")

    def test_mixed_positions(self):
        pos1 = _make_position(qty="10", avg_price="100")
        pos2 = _make_position(qty="-5", avg_price="200")
        snapshot = PortfolioSnapshot(positions=[pos1, pos2])
        assert snapshot.net_exposure.amount == Decimal("2000")

    def test_empty_positions(self):
        snapshot = PortfolioSnapshot(positions=[])
        assert snapshot.net_exposure.amount == Decimal("0")


class TestPortfolioSnapshotLen:
    def test_len(self):
        pos1 = _make_position()
        pos2 = _make_position()
        snapshot = PortfolioSnapshot(positions=[pos1, pos2])
        assert len(snapshot) == 2

    def test_len_empty(self):
        snapshot = PortfolioSnapshot(positions=[])
        assert len(snapshot) == 0
