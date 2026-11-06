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
from tradex_domain import Candle, Equity, Price, Quantity, Quote, Timeframe
from tradex_domain.events import OrderFilled
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


@pytest.mark.xfail(strict=True, reason="A4: RevisedBar unhandled")
def test_sba_late_tick_parity(bus: ReactiveBus) -> None:
    aggregator = wire_bar_aggregation(bus, [INSTRUMENT], now=lambda: 0.0)

    bars_seen: list[Candle] = []
    bus.of_type(Candle).subscribe(lambda c: bars_seen.append(c))

    fills_seen: list[OrderFilled] = []
    bus.of_type(OrderFilled).subscribe(lambda e: fills_seen.append(e))

    try:
        # Minute 10:15: two prints.
        bus.publish(_quote(_stamp("10:15:00"), "100.00"))
        bus.publish(_quote(_stamp("10:15:20"), "100.40"))
        # Minute 10:16: closes the 10:15 bar.
        bus.publish(_quote(_stamp("10:16:00"), "102.00"))

        # (a) strategy sees the bar close.
        assert any(c.ohlc.close.value == Decimal("100.40") for c in bars_seen), (
            "the 10:15 bar close never reached the strategy"
        )

        fills_after_close = len(fills_seen)

        # Cross-minute late tick for the already-closed 10:15 bar (inside the
        # 60s revision window because the clock is pinned at 0).
        bus.publish(_quote(_stamp("10:15:10"), "103.00"))

        # (c) no duplicate fills appear after the revision attempt.
        assert len(fills_seen) == fills_after_close, (
            "a late tick produced an extra fill"
        )

        # (b) desired: the revised bar reaches the strategy/chart. Today it
        # does not (RevisedBar is unhandled) -- pinned as xfail.
        revised_seen = any(
            c.ohlc.close.value == Decimal("103.00")
            and c.timestamp.astimezone(IST).replace(tzinfo=None).hour == 10
            and c.timestamp.astimezone(IST).replace(tzinfo=None).minute == 15
            for c in bars_seen
        )
        assert revised_seen, "revised bar did not reach the strategy/chart (A4)"
    finally:
        aggregator.dispose()
