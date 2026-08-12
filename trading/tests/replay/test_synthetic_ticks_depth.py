"""Tests for Depth emission in SyntheticTickGenerator."""

from __future__ import annotations

from datetime import UTC, datetime
from decimal import Decimal

from tradex_domain import OHLC, Candle, Depth, Quote, Timeframe
from tradex_domain.instruments import Equity
from tradex_domain.value_objects import Price, Quantity

from tradex_trading.reactive.bus import ReactiveBus
from tradex_trading.replay.synthetic_ticks import SyntheticTickGenerator

INSTRUMENT = Equity.of("NSE", "RELIANCE")


def _candle(**kwargs) -> Candle:
    defaults = dict(
        open_=Decimal("100"), high=Decimal("110"),
        low=Decimal("95"), close=Decimal("105"),
        volume=Decimal("10000"),
    )
    defaults.update(kwargs)
    return Candle(
        instrument=INSTRUMENT,
        timeframe=Timeframe.M1,
        ohlc=OHLC(
            open=Price(value=defaults["open_"]),
            high=Price(value=defaults["high"]),
            low=Price(value=defaults["low"]),
            close=Price(value=defaults["close"]),
        ),
        volume=Quantity(value=defaults["volume"]),
        timestamp=datetime(2026, 8, 8, 9, 15, 0, tzinfo=UTC),
    )


class TestDepthEmission:
    def test_no_depth_by_default(self) -> None:
        """Default mode: only Quotes, no Depth events."""
        events: list[object] = []
        bus = ReactiveBus(message_log=events)
        SyntheticTickGenerator(bus, seed=1).feed_bar(_candle())
        depths = [e for e in events if isinstance(e, Depth)]
        assert len(depths) == 0

    def test_depth_emitted_when_levels_set(self) -> None:
        """depth_levels=5 → one Depth event per bar."""
        events: list[object] = []
        bus = ReactiveBus(message_log=events)
        gen = SyntheticTickGenerator(bus, seed=1, depth_levels=5)
        gen.feed_bar(_candle())
        depths = [e for e in events if isinstance(e, Depth)]
        assert len(depths) == 1

    def test_depth_has_correct_levels(self) -> None:
        """Depth has exactly the requested number of bid and ask levels."""
        events: list[object] = []
        bus = ReactiveBus(message_log=events)
        gen = SyntheticTickGenerator(bus, seed=1, depth_levels=5)
        gen.feed_bar(_candle())
        depth = next(e for e in events if isinstance(e, Depth))
        assert len(depth.bids) == 5
        assert len(depth.asks) == 5

    def test_depth_bids_descending_asks_ascending(self) -> None:
        """Book ordering invariant holds."""
        events: list[object] = []
        bus = ReactiveBus(message_log=events)
        gen = SyntheticTickGenerator(bus, seed=1, depth_levels=10)
        gen.feed_bar(_candle())
        depth = next(e for e in events if isinstance(e, Depth))
        for i in range(len(depth.bids) - 1):
            assert depth.bids[i][0].value >= depth.bids[i + 1][0].value
        for i in range(len(depth.asks) - 1):
            assert depth.asks[i][0].value <= depth.asks[i + 1][0].value

    def test_depth_best_bid_ask_around_ltp(self) -> None:
        """Best bid < last tick price < best ask."""
        events: list[object] = []
        bus = ReactiveBus(message_log=events)
        gen = SyntheticTickGenerator(bus, seed=1, depth_levels=5)
        gen.feed_bar(_candle())
        quotes = [e for e in events if isinstance(e, Quote)]
        depth = next(e for e in events if isinstance(e, Depth))
        last_ltp = quotes[-1].ltp.value
        assert depth.bids[0][0].value < last_ltp
        assert depth.asks[0][0].value > last_ltp

    def test_depth_quantities_positive(self) -> None:
        """All level quantities are positive."""
        events: list[object] = []
        bus = ReactiveBus(message_log=events)
        gen = SyntheticTickGenerator(bus, seed=1, depth_levels=5)
        gen.feed_bar(_candle())
        depth = next(e for e in events if isinstance(e, Depth))
        for _, qty in depth.bids:
            assert qty.value > 0
        for _, qty in depth.asks:
            assert qty.value > 0

    def test_depth_emitted_per_bar_in_replay(self) -> None:
        """Two bars → two Depth events."""
        events: list[object] = []
        bus = ReactiveBus(message_log=events)
        gen = SyntheticTickGenerator(bus, seed=1, depth_levels=5)
        gen.feed_bar(_candle())
        gen.feed_bar(_candle(close=Decimal("108")))
        depths = [e for e in events if isinstance(e, Depth)]
        assert len(depths) == 2

    def test_depth_timestamp_matches_last_tick(self) -> None:
        """The Depth snapshot is stamped with the last tick's timestamp —
        it snapshots the book at bar close, not one second later."""
        events: list[object] = []
        bus = ReactiveBus(message_log=events)
        gen = SyntheticTickGenerator(bus, seed=1, depth_levels=5)
        gen.feed_bar(_candle())
        quotes = [e for e in events if isinstance(e, Quote)]
        depth = next(e for e in events if isinstance(e, Depth))
        assert depth.timestamp == quotes[-1].timestamp
