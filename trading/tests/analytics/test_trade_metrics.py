"""Round-trip trade derivation + statistics block (backtest results UI contract).

Covers the two things the backtest result needed and did not have: pairing a
fill stream into closed round trips (FIFO, partial exits, reversals), and the
TradingView-style metrics block computed from those round trips.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from decimal import Decimal

import pytest
from tradex_domain import OHLC, Candle, Timeframe
from tradex_domain.enums import OrderSide
from tradex_domain.execution import Fill
from tradex_domain.instruments import Equity
from tradex_domain.value_objects import OrderId, Price, Quantity

from tradex_trading.analytics import (
    compute_statistics,
    round_trip_trades,
    sortino_ratio,
)

RELIANCE = Equity.of("NSE", "RELIANCE")
T0 = datetime(2026, 9, 1, 9, 15, tzinfo=UTC)


def _fill(side: OrderSide, qty: str, price: str, minutes: int, order: str) -> Fill:
    """One fill *minutes* after T0."""
    return Fill(
        order_id=OrderId(value=order),
        instrument=RELIANCE,
        side=side,
        quantity=Quantity(value=Decimal(qty)),
        price=Price(value=Decimal(price)),
        timestamp=T0 + timedelta(minutes=minutes),
    )


def _bar(minutes: int, high: str, low: str) -> Candle:
    """A 1-minute bar with an explicit high/low and a flat open/close."""
    h, low = Decimal(high), Decimal(low)
    return Candle(
        instrument=RELIANCE,
        timeframe=Timeframe.M1,
        ohlc=OHLC(
            open=Price(value=low),
            high=Price(value=h),
            low=Price(value=low),
            close=Price(value=h),
        ),
        volume=Quantity(value=Decimal("100")),
        timestamp=T0 + timedelta(minutes=minutes),
    )


class TestRoundTripTrades:
    def test_long_round_trip(self):
        """Buy then sell produces one LONG trade with the right P&L."""
        trades = round_trip_trades(
            [
                _fill(OrderSide.BUY, "10", "100", 0, "e1"),
                _fill(OrderSide.SELL, "10", "110", 5, "x1"),
            ]
        )

        assert len(trades) == 1
        trade = trades[0]
        assert (trade.symbol, trade.side) == ("RELIANCE", "LONG")
        assert trade.quantity == Decimal("10")
        assert trade.entry_price == Decimal("100")
        assert trade.exit_price == Decimal("110")
        assert trade.gross_pnl == Decimal("100.00")  # 10 * 10
        assert trade.net_pnl == Decimal("100.00")  # no costs supplied
        assert trade.holding_period == timedelta(minutes=5)
        assert trade.is_win

    def test_short_round_trip(self):
        """Sell then buy back produces a SHORT trade that profits on a fall."""
        trades = round_trip_trades(
            [
                _fill(OrderSide.SELL, "5", "200", 0, "e1"),
                _fill(OrderSide.BUY, "5", "180", 3, "x1"),
            ]
        )

        assert len(trades) == 1
        assert trades[0].side == "SHORT"
        assert trades[0].gross_pnl == Decimal("100.00")  # 5 * 20

    def test_partial_exits_split_into_fifo_trades(self):
        """A scale-in closed in two pieces yields one trade per matched lot."""
        trades = round_trip_trades(
            [
                _fill(OrderSide.BUY, "10", "100", 0, "e1"),
                _fill(OrderSide.BUY, "10", "120", 1, "e2"),
                _fill(OrderSide.SELL, "10", "130", 2, "x1"),
                _fill(OrderSide.SELL, "10", "140", 3, "x2"),
            ]
        )

        assert len(trades) == 2
        # FIFO: the 100 lot exits first, then the 120 lot.
        assert [t.entry_price for t in trades] == [Decimal("100"), Decimal("120")]
        assert [t.exit_price for t in trades] == [Decimal("130"), Decimal("140")]
        assert [t.gross_pnl for t in trades] == [
            Decimal("300.00"),  # (130-100)*10
            Decimal("200.00"),  # (140-120)*10
        ]

    def test_reversal_closes_then_opens(self):
        """An oversized opposing fill closes the lot and opens the remainder."""
        trades = round_trip_trades(
            [
                _fill(OrderSide.BUY, "10", "100", 0, "e1"),
                _fill(OrderSide.SELL, "25", "110", 1, "x1"),
                _fill(OrderSide.BUY, "15", "105", 2, "x2"),
            ]
        )

        assert len(trades) == 2
        assert trades[0].quantity == Decimal("10")
        assert trades[0].gross_pnl == Decimal("100.00")
        # Remainder of the sell opened a 15-lot short, closed at 105.
        assert trades[1].side == "SHORT"
        assert trades[1].quantity == Decimal("15")
        assert trades[1].gross_pnl == Decimal("75.00")  # (110-105)*15

    def test_open_position_is_not_reported(self):
        """A lot that never closes stays open and yields no trade."""
        trades = round_trip_trades(
            [
                _fill(OrderSide.BUY, "10", "100", 0, "e1"),
                _fill(OrderSide.SELL, "4", "110", 1, "x1"),
            ]
        )

        assert len(trades) == 1  # 4 of the 10 closed; 6 still open.

    def test_costs_attribute_proportionally(self):
        """Order-level costs land on the matched portion only."""
        trades = round_trip_trades(
            [
                _fill(OrderSide.BUY, "10", "100", 0, "e1"),
                _fill(OrderSide.SELL, "4", "110", 1, "x1"),
                _fill(OrderSide.SELL, "6", "110", 2, "x2"),
            ],
            cost_by_order={"e1": Decimal("10"), "x1": Decimal("4"), "x2": Decimal("6")},
        )

        # e1's 10-unit cost splits 4/6 by matched quantity; each exit
        # contributes its own cost in full (it is fully matched).
        assert trades[0].costs == Decimal("8.00")  # 4/10*10 + 4
        assert trades[1].costs == Decimal("12.00")  # 6/10*10 + 6
        assert trades[0].net_pnl == Decimal("32.00")  # 40 gross - 8.00

    def test_exit_reason_is_stamped(self):
        """Exit reasons from the order map ride onto the closed trade."""
        trades = round_trip_trades(
            [
                _fill(OrderSide.BUY, "10", "100", 0, "e1"),
                _fill(OrderSide.SELL, "10", "95", 1, "x1"),
            ],
            reason_by_order={"x1": "stop_loss"},
        )

        assert trades[0].exit_reason == "stop_loss"
        assert not trades[0].is_win

    def test_mae_mfe_measured_from_bars(self):
        """Excursions use the bars inside the holding window, per side."""
        bars = {
            "RELIANCE": [
                _bar(0, high="101", low="99"),
                _bar(2, high="118", low="96"),  # the adverse/favourable extremes
                _bar(5, high="112", low="108"),
            ]
        }
        trades = round_trip_trades(
            [
                _fill(OrderSide.BUY, "10", "100", 0, "e1"),
                _fill(OrderSide.SELL, "10", "110", 5, "x1"),
            ],
            bars_by_symbol=bars,
        )

        # Long from 100: low of 96 -> MAE 4, high of 118 -> MFE 18.
        assert trades[0].mae == Decimal("4.00")
        assert trades[0].mfe == Decimal("18.00")

    def test_mae_mfe_absent_without_bars(self):
        """No bar data means no excursion claim rather than a wrong one."""
        trades = round_trip_trades(
            [
                _fill(OrderSide.BUY, "10", "100", 0, "e1"),
                _fill(OrderSide.SELL, "10", "110", 5, "x1"),
            ]
        )

        assert trades[0].mae is None
        assert trades[0].mfe is None


class TestComputeStatistics:
    def _trades(self, pnls: list[float]):
        """Build round trips with the given net P&L outcomes."""
        fills = []
        for i, pnl in enumerate(pnls):
            exit_price = 100 + pnl
            fills.append(_fill(OrderSide.BUY, "1", "100", i * 3, f"e{i}"))
            fills.append(_fill(OrderSide.SELL, "1", str(exit_price), i * 3 + 1, f"x{i}"))
        return round_trip_trades(fills)

    def test_empty_trades_gives_zeroed_block(self):
        """An empty run reports zeros, not an exception or NaN."""
        stats = compute_statistics([])

        assert stats.total_trades == 0
        assert stats.net_profit == Decimal("0.00")
        assert stats.profit_factor == 0.0
        assert stats.win_rate == 0.0
        assert stats.risk_reward == 0.0

    def test_counts_and_profit_factor(self):
        """Winners/losers split and profit factor follow the net P&L list."""
        stats = compute_statistics(self._trades([10, -5, 20, -5]))

        assert stats.total_trades == 4
        assert stats.winning_trades == 2
        assert stats.losing_trades == 2
        assert stats.gross_profit == Decimal("30.00")
        assert stats.gross_loss == Decimal("-10.00")
        assert stats.profit_factor == 3.0  # 30 / 10
        assert stats.win_rate == 0.5
        assert stats.loss_rate == 0.5

    def test_averages_largest_and_expectancy(self):
        """Average trade, winners/losers, extremes and expectancy agree."""
        stats = compute_statistics(self._trades([10, -5, 20, -5]))

        assert stats.net_profit == Decimal("20.00")
        assert stats.average_trade == Decimal("5.00")  # 20 / 4
        assert stats.average_winner == Decimal("15.00")  # (10 + 20) / 2
        assert stats.average_loser == Decimal("-5.00")
        assert stats.largest_win == Decimal("20.00")
        assert stats.largest_loss == Decimal("-5.00")
        assert stats.expectancy == Decimal("5.00")
        assert stats.risk_reward == 3.0  # 15 / 5

    def test_no_losers_does_not_divide_by_zero(self):
        """An all-winner run has an undefined profit factor, reported as 0."""
        stats = compute_statistics(self._trades([10, 20]))

        assert stats.profit_factor == 0.0
        assert stats.gross_loss == Decimal("0.00")
        assert stats.risk_reward == 0.0
        assert stats.win_rate == 1.0

    def test_drawdown_and_exposure_from_equity_curve(self):
        """Drawdown comes from the curve; exposure from the holding window."""
        stats = compute_statistics(
            self._trades([10, -5, 20, -5]),
            equity_curve=[100, 120, 90, 110],
        )

        assert stats.max_drawdown == pytest.approx(-0.25)  # 120 -> 90
        assert stats.max_drawdown_pct == pytest.approx(-25.0)
        # Four 1-minute holds inside a 10-minute first-entry..last-exit span.
        assert stats.exposure == pytest.approx(0.4)

    def test_sharpe_and_sortino_signs_follow_the_curve(self):
        """A net-rising curve with a dip scores positively on both ratios."""
        stats = compute_statistics(
            self._trades([10, -5, 20, -5]),
            equity_curve=[100, 103, 101, 105, 108],
        )

        assert stats.sharpe > 0
        assert stats.sortino > 0

    def test_sortino_ignores_upside_volatility(self):
        """Adding upside-only volatility must not change the Sortino denominator.

        Both series share the same downside step; the second adds a large
        *upside* swing, which Sortino must not penalise.
        """
        flat = sortino_ratio([0.01, -0.02, 0.01, 0.01])
        spiky = sortino_ratio([0.01, -0.02, 0.09, 0.01])

        assert spiky > flat

    def test_sortino_zero_without_downside(self):
        """No losing periods means no downside deviation to divide by."""
        assert sortino_ratio([0.01, 0.02, 0.03]) == 0.0
        assert sortino_ratio([]) == 0.0
        assert sortino_ratio([0.01]) == 0.0

    def test_to_dict_is_json_ready(self):
        """The API boundary is float/int, never Decimal."""
        payload = compute_statistics(self._trades([10, -5])).to_dict()

        assert isinstance(payload["total_trades"], int)
        assert isinstance(payload["net_profit"], float)
        assert set(payload) >= {
            "profit_factor",
            "win_rate",
            "expectancy",
            "risk_reward",
            "max_drawdown_pct",
            "sharpe",
            "sortino",
            "exposure",
        }
