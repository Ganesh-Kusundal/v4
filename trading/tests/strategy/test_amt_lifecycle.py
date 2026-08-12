"""Tests for AMTStrategy lifecycle: exit signals, loss counting, circuit breaker."""

from __future__ import annotations

from datetime import UTC, datetime
from decimal import Decimal

from tradex_domain.enums import OrderSide
from tradex_domain.execution import Fill
from tradex_domain.instruments import Equity
from tradex_domain.market import Quote
from tradex_domain.strategy import Signal, StrategyContext
from tradex_domain.value_objects import OrderId, Price, Quantity

from tradex_trading.strategy.amt.strategy import AMTStrategy

INSTRUMENT = Equity.of("NSE", "RELIANCE")


def _fill(side: OrderSide, price: float, qty: int = 1) -> Fill:
    return Fill(
        order_id=OrderId("O1"),
        instrument=INSTRUMENT,
        side=side,
        quantity=Quantity(value=Decimal(str(qty))),
        price=Price(value=Decimal(str(price))),
        timestamp=datetime(2026, 8, 8, 9, 15, 0, tzinfo=UTC),
    )


def _quote(ltp: float, bid: float, ask: float) -> Quote:
    return Quote(
        instrument=INSTRUMENT,
        ltp=Price(value=Decimal(str(ltp))),
        bid=Price(value=Decimal(str(bid))),
        ask=Price(value=Decimal(str(ask))),
        volume=Quantity(value=Decimal("100")),
        timestamp=datetime(2026, 8, 8, 9, 15, 0, tzinfo=UTC),
    )


def _ctx() -> StrategyContext:
    return StrategyContext()


class TestAmtLossCounting:
    def test_winning_round_trip_does_not_count_loss(self) -> None:
        s = AMTStrategy()
        s.on_start(_ctx())
        # Simulate entry: long at 100
        s._in_position = True
        s._entry_side = OrderSide.BUY
        s.on_fill(_ctx(), _fill(OrderSide.BUY, 100.0))  # entry fill
        s.on_fill(_ctx(), _fill(OrderSide.SELL, 105.0))  # exit above entry — win
        assert s._daily_losses == 0

    def test_losing_long_round_trip_counts_loss(self) -> None:
        s = AMTStrategy()
        s.on_start(_ctx())
        s._in_position = True
        s._entry_side = OrderSide.BUY
        s.on_fill(_ctx(), _fill(OrderSide.BUY, 100.0))
        s.on_fill(_ctx(), _fill(OrderSide.SELL, 95.0))  # exit below entry — loss
        assert s._daily_losses == 1
        # Round-trip is closed: state cleared
        assert s._entry_side is None
        assert s._entry_price is None

    def test_losing_short_round_trip_counts_loss(self) -> None:
        s = AMTStrategy()
        s.on_start(_ctx())
        s._in_position = True
        s._entry_side = OrderSide.SELL
        s.on_fill(_ctx(), _fill(OrderSide.SELL, 100.0))
        s.on_fill(_ctx(), _fill(OrderSide.BUY, 110.0))  # exit above short — loss
        assert s._daily_losses == 1

    def test_circuit_breaker_halts_after_max_losses(self) -> None:
        s = AMTStrategy(max_daily_losses=1)
        s.on_start(_ctx())
        s._in_position = True
        s._entry_side = OrderSide.BUY
        s.on_fill(_ctx(), _fill(OrderSide.BUY, 100.0))
        s.on_fill(_ctx(), _fill(OrderSide.SELL, 90.0))  # loss → breaker trips
        assert s._daily_losses == 1
        assert s._daily_losses >= s._max_daily_losses


class TestAmtExitSignal:
    def test_long_exits_on_bearish_flip(self) -> None:
        s = AMTStrategy()
        s.on_start(_ctx())
        # Enter a long with a bearish trailing window (5 × -100)
        s._in_position = True
        s._entry_side = OrderSide.BUY
        for _ in range(5):
            s._recent_deltas.append(-100)
        # A quote at a key level should trigger a SELL exit. Its own delta is
        # 0 (ltp == mid), so the trailing window stays bearish (-400).
        signal = s.on_quote(_ctx(), _quote(100.0, 99.5, 100.5))
        assert isinstance(signal, Signal)
        assert signal.direction == OrderSide.SELL
        assert s._in_position is False
        # But the round-trip is still open until the exit fill lands
        assert s._entry_side == OrderSide.BUY

    def test_no_exit_when_direction_neutral(self) -> None:
        s = AMTStrategy()
        s.on_start(_ctx())
        s._in_position = True
        s._entry_side = OrderSide.BUY
        # Seed a window that stays neutral after the quote's own delta enters.
        # The quote trades at mid (ltp 100.0 == mid 100.0), so its delta is 0;
        # the seeded [+100,-100,+100,-100,+100] window sums to 0 after the
        # shift → neutral → no exit.
        s._recent_deltas.extend([100, -100, 100, -100, 100])
        assert s.on_quote(_ctx(), _quote(100.0, 99.5, 100.5)) is None
        assert s._in_position is True


class TestReentryGuard:
    def test_no_reentry_while_exit_fill_pending(self) -> None:
        """An exit signal leaves the round-trip open — new entries are blocked."""
        s = AMTStrategy()
        s.on_start(_ctx())
        # Long with a bearish window → exit fires, round-trip still open
        s._in_position = True
        s._entry_side = OrderSide.BUY
        for _ in range(5):
            s._recent_deltas.append(-100)
        signal = s.on_quote(_ctx(), _quote(100.0, 99.5, 100.5))
        assert isinstance(signal, Signal)  # SELL exit emitted
        assert s._in_position is False
        assert s._entry_side == OrderSide.BUY  # unclosed until exit fill

        # Even a strong bullish window must not open a new position now
        s._recent_deltas.extend([100, 100, 100, 100, 100])
        assert s.on_quote(_ctx(), _quote(100.5, 100.0, 100.5)) is None
        assert s._entry_side == OrderSide.BUY  # still blocked

    def test_exit_fill_closes_round_trip_then_reentry_allowed(self) -> None:
        """A closed round-trip clears the guard, so a new entry can fire."""
        s = AMTStrategy()
        s.on_start(_ctx())
        s._in_position = True
        s._entry_side = OrderSide.BUY
        s.on_fill(_ctx(), _fill(OrderSide.BUY, 100.0))  # entry fill
        s.on_fill(_ctx(), _fill(OrderSide.SELL, 105.0))  # exit fill (win)
        assert s._entry_side is None  # guard cleared
        assert s._daily_losses == 0

        # A bullish window + key level + aggression should now be able to
        # open a fresh position. Profile POC sits on 100.5, the quote price.
        s._profile = {100.0: 500.0, 100.5: 2000.0, 101.0: 500.0}
        s._recent_deltas.extend([100, 100, 100, 100, 100])
        sig = s.on_quote(_ctx(), _quote(100.5, 100.0, 100.5))  # buy at ask
        assert isinstance(sig, Signal)
        assert sig.direction == OrderSide.BUY
        assert s._entry_side == OrderSide.BUY
        assert s._entry_price is None  # fresh round-trip — recorded at fill


class TestRunningCvd:
    def test_recent_deltas_window_is_bounded(self) -> None:
        s = AMTStrategy()
        s.on_start(_ctx())
        for _ in range(20):
            s._recent_deltas.append(100)
        assert len(s._recent_deltas) == 5  # bounded window

    def test_deltas_accumulate_across_quotes(self) -> None:
        s = AMTStrategy()
        s.on_start(_ctx())
        s._recent_deltas.append(s._quote_delta(_quote(100.0, 99.5, 100.0)))  # buy at ask
        s._recent_deltas.append(s._quote_delta(_quote(101.0, 100.5, 101.0)))  # buy at ask
        assert sum(s._recent_deltas) == 200

    def test_quote_delta_ignores_mid_trades(self) -> None:
        s = AMTStrategy()
        s.on_start(_ctx())
        assert s._quote_delta(_quote(100.0, 99.5, 100.5)) == 0  # ltp == mid
        assert s._quote_delta(_quote(100.5, 100.0, 100.5)) == 100  # buy at ask
        assert s._quote_delta(_quote(100.0, 100.0, 100.5)) == -100  # sell at bid
