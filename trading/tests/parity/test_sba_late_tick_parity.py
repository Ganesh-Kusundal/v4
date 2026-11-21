"""Parity: SessionBarAggregator late-tick revision on a recorded tape (A4).

Pins the current (broken) behaviour: a revised bar emitted by
``BarAggregator._revise_closed`` is delivered as a ``RevisedBar`` to the
frame publisher, which cannot read ``.closed`` on it, so the revision is
swallowed by bus isolation and reaches no strategy/chart. Traded-bar freeze
(``mark_filled``) is likewise unenforced in production because nothing calls
it. See audit A4 / D-3.
"""

from __future__ import annotations

from datetime import datetime
from decimal import Decimal
from zoneinfo import ZoneInfo

import pytest
from tradex_domain import Candle, Equity, Price, Quantity, Quote
from tradex_reactive.bus import ReactiveBus
from tradex_runtime.bar_aggregator import wire_bar_aggregation

IST = ZoneInfo("Asia/Kolkata")
UTC = ZoneInfo("UTC")
INSTRUMENT = Equity.of("NSE", "RELIANCE")


def _stamp(hms: str) -> datetime:
    hour, minute, second = (int(part) for part in hms.split(":"))
    return datetime(2026, 9, 24, hour, minute, second, tzinfo=IST)


def _quote(ts: datetime, ltp: str) -> Quote:
    return Quote(
        instrument=INSTRUMENT,
        ltp=Price(value=Decimal(ltp)),
        volume=Quantity(value=Decimal("1000")),
        timestamp=ts,
        exchange="NSE",
        provider="sim",
    )


@pytest.fixture
def bus() -> ReactiveBus:
    return ReactiveBus()


def test_sba_late_tick_parity(bus: ReactiveBus) -> None:
    from tradex_domain.events import BarRevised

    aggregator = wire_bar_aggregation(bus, [INSTRUMENT], now=lambda: 0.0)
    bars_seen: list[Candle] = []
    revisions_seen: list[BarRevised] = []
    bus.of_type(Candle).subscribe(bars_seen.append)
    bus.of_type(BarRevised).subscribe(revisions_seen.append)

    try:
        bus.publish(_quote(_stamp("10:15:00"), "100.00"))
        bus.publish(_quote(_stamp("10:15:20"), "100.40"))
        bus.publish(_quote(_stamp("10:16:00"), "102.00"))

        closed_bars = len(bars_seen)
        assert any(c.ohlc.close.value == Decimal("100.40") for c in bars_seen)

        # Cross-minute late tick for the already-closed 10:15 bar (inside the
        # 60s revision window because the clock is pinned at 0).
        bus.publish(_quote(_stamp("10:15:10"), "103.00"))

        assert len(bars_seen) == closed_bars, "a revision must not look like a new bar"
        assert revisions_seen, "revised bar did not reach any consumer (A4)"
        assert revisions_seen[-1].frame.close == pytest.approx(103.0)
        assert revisions_seen[-1].reason == "late_tick"
    finally:
        aggregator.dispose()


def test_revision_is_not_a_feed_integrity_violation(bus: ReactiveBus) -> None:
    """A correction to an older bar is not an out-of-order feed event.

    The integrity tracker owns the *sequence* contract: a revised bar has a
    timestamp the tracker has already passed, so handing it the revision
    counted a false OUT_OF_ORDER and could trip fail-closed recovery.
    """
    from tradex_runtime.feed_integrity import FeedIntegrityTracker

    tracker = FeedIntegrityTracker()
    aggregator = wire_bar_aggregation(
        bus, [INSTRUMENT], now=lambda: 0.0, on_bar_frame=tracker.on_bar
    )
    try:
        bus.publish(_quote(_stamp("10:15:00"), "100.00"))
        bus.publish(_quote(_stamp("10:16:00"), "102.00"))
        bus.publish(_quote(_stamp("10:17:00"), "102.50"))

        bus.publish(_quote(_stamp("10:15:10"), "103.00"))

        assert tracker.snapshot().out_of_order_events == 0
    finally:
        aggregator.dispose()
