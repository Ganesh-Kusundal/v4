"""Tests for SyntheticTickGenerator — M1 candles -> synthetic Quote ticks."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from decimal import Decimal

import pytest
from tradex_domain import OHLC, Candle, Quote, Timeframe
from tradex_domain.instruments import Equity
from tradex_domain.value_objects import Price, Quantity

from tradex_trading.reactive.bus import ReactiveBus
from tradex_trading.replay.backtest import FakeClock
from tradex_trading.replay.synthetic_ticks import SyntheticTickGenerator

INSTRUMENT = Equity.of("NSE", "RELIANCE")


def _candle(
    *,
    timeframe=Timeframe.M1,
    open_=Decimal("100"),
    high=Decimal("110"),
    low=Decimal("95"),
    close=Decimal("105"),
    volume=Decimal("10000"),
) -> Candle:
    return Candle(
        instrument=INSTRUMENT,
        timeframe=timeframe,
        ohlc=OHLC(
            open=Price(value=open_),
            high=Price(value=high),
            low=Price(value=low),
            close=Price(value=close),
        ),
        volume=Quantity(value=volume),
        timestamp=datetime(2026, 8, 7, 9, 15, 0, tzinfo=UTC),
    )


class TestSyntheticTickGenerator:
    def test_m1_only_guard(self) -> None:
        gen = SyntheticTickGenerator(ReactiveBus(), seed=1)
        with pytest.raises(ValueError, match="M1"):
            gen.feed_bar(_candle(timeframe=Timeframe.D1))

    def test_publishes_one_quote_per_second_with_bar_timestamps(self) -> None:
        events: list[object] = []
        bus = ReactiveBus(message_log=events)
        gen = SyntheticTickGenerator(bus, seed=1)
        candle = _candle()
        gen.feed_bar(candle)

        quotes = [e for e in events if isinstance(e, Quote)]
        assert len(quotes) == 60
        for i, quote in enumerate(quotes):
            assert quote.timestamp == candle.timestamp + timedelta(seconds=i)
            assert quote.instrument.instrument_id == INSTRUMENT.instrument_id

    def test_first_tick_is_open_last_tick_is_close(self) -> None:
        events: list[object] = []
        bus = ReactiveBus(message_log=events)
        gen = SyntheticTickGenerator(bus, seed=1)
        candle = _candle()
        gen.feed_bar(candle)

        quotes = [e for e in events if isinstance(e, Quote)]
        assert quotes[0].ltp.value == candle.ohlc.open.value
        assert quotes[-1].ltp.value == candle.ohlc.close.value

    def test_all_ticks_stay_within_bar_range(self) -> None:
        events: list[object] = []
        bus = ReactiveBus(message_log=events)
        gen = SyntheticTickGenerator(bus, seed=7)
        candle = _candle(open_=Decimal("100"), high=Decimal("110"),
                         low=Decimal("95"), close=Decimal("105"))
        gen.feed_bar(candle)

        low_, high_ = Decimal("95"), Decimal("110")
        for quote in (e for e in events if isinstance(e, Quote)):
            assert low_ <= quote.ltp.value <= high_
            assert quote.bid.value < quote.ask.value

    def test_tick_volumes_sum_exactly_to_bar_volume(self) -> None:
        events: list[object] = []
        bus = ReactiveBus(message_log=events)
        gen = SyntheticTickGenerator(bus, seed=1)
        candle = _candle(volume=Decimal("10000"))
        gen.feed_bar(candle)

        quotes = [e for e in events if isinstance(e, Quote)]
        total = sum((q.volume.value for q in quotes), Decimal("0"))
        # The last tick absorbs the Decimal rounding remainder, so the
        # split sums exactly to the bar volume.
        assert total == candle.volume.value

    def test_clock_advances_one_second_per_tick(self) -> None:
        clock = FakeClock(start=datetime(2026, 8, 7, 9, 15, 0, tzinfo=UTC))
        gen = SyntheticTickGenerator(ReactiveBus(), clock=clock, seed=1)
        gen.feed_bar(_candle())
        assert clock.now() == datetime(2026, 8, 7, 9, 16, 0, tzinfo=UTC)

    def test_close_outside_range_is_clamped(self) -> None:
        """Malformed candle (close outside [low, high]) honors the clamp."""
        events: list[object] = []
        bus = ReactiveBus(message_log=events)
        gen = SyntheticTickGenerator(bus, seed=1)
        gen.feed_bar(_candle(open_=Decimal("100"), high=Decimal("110"),
                             low=Decimal("95"), close=Decimal("200")))
        quotes = [e for e in events if isinstance(e, Quote)]
        assert len(quotes) == 60
        assert all(Decimal("95") <= q.ltp.value <= Decimal("110") for q in quotes)

    def test_flat_bar_emits_flat_ticks(self) -> None:
        events: list[object] = []
        bus = ReactiveBus(message_log=events)
        gen = SyntheticTickGenerator(bus, seed=1)
        gen.feed_bar(_candle(open_=Decimal("100"), high=Decimal("100"),
                             low=Decimal("100"), close=Decimal("100")))
        quotes = [e for e in events if isinstance(e, Quote)]
        assert len(quotes) == 60
        assert all(q.ltp.value == Decimal("100") for q in quotes)

    def test_reproducible_with_seed(self) -> None:
        def prices() -> list[Decimal]:
            events: list[object] = []
            bus = ReactiveBus(message_log=events)
            SyntheticTickGenerator(bus, seed=42).feed_bar(_candle())
            return [q.ltp.value for q in events if isinstance(q, Quote)]

        assert prices() == prices()
