"""Tests for feed integrity accounting (runtime.feed_integrity)."""

from __future__ import annotations

from datetime import UTC, datetime
from decimal import Decimal

from tradex_domain.instruments import Equity
from tradex_domain.market import Quote
from tradex_domain.value_objects import Price, Quantity

from tradex_trading.runtime.bar_aggregator import BarFrame
from tradex_trading.runtime.feed_integrity import (
    FeedIntegrityTracker,
    GapKind,
)
from tradex_trading.runtime.metrics import MetricsRegistry

_INSIDE = datetime(2026, 9, 24, 4, 0, tzinfo=UTC)  # 09:30 IST
_OUTSIDE = datetime(2026, 9, 24, 2, 0, tzinfo=UTC)  # 07:30 IST


def _quote(
    ts: datetime,
    *,
    ltp: str = "100",
    symbol: str = "RELIANCE",
    volume: str | None = None,
    metadata: dict[str, object] | None = None,
) -> Quote:
    return Quote(
        instrument=Equity.of("NSE", symbol),
        ltp=Price(value=Decimal(ltp)),
        volume=Quantity(value=Decimal(volume)) if volume is not None else None,
        timestamp=ts,
        metadata=metadata,
    )


def _bar(epoch: int, *, symbol: str = "RELIANCE", closed: bool = True) -> BarFrame:
    return BarFrame(
        instrument=f"NSE:{symbol}",
        timeframe="1m",
        time=epoch,
        open=1.0,
        high=1.0,
        low=1.0,
        close=1.0,
        volume=1.0,
        closed=closed,
    )


def test_duplicate_quote_counted() -> None:
    tracker = FeedIntegrityTracker()
    quote = _quote(_INSIDE)

    assert tracker.on_quote(quote) == ()
    assert tracker.on_quote(quote) == (GapKind.DUPLICATE_EVENT,)

    snapshot = tracker.snapshot()
    assert snapshot.events_seen == 2
    assert snapshot.duplicate_events == 1


def test_provider_event_id_detects_duplicate_with_new_timestamp() -> None:
    tracker = FeedIntegrityTracker()

    tracker.on_quote(_quote(_INSIDE, metadata={"event_id": "abc-1"}))
    violations = tracker.on_quote(
        _quote(datetime(2026, 9, 24, 4, 1, tzinfo=UTC), metadata={"event_id": "abc-1"})
    )

    assert GapKind.DUPLICATE_EVENT in violations


def test_out_of_order_quote_counted_without_moving_last_ts() -> None:
    tracker = FeedIntegrityTracker()
    newer = datetime(2026, 9, 24, 4, 5, tzinfo=UTC)
    tracker.on_quote(_quote(_INSIDE))
    tracker.on_quote(_quote(newer))

    violations = tracker.on_quote(_quote(_INSIDE))

    assert GapKind.OUT_OF_ORDER in violations
    assert tracker.snapshot().out_of_order_events == 1


def test_large_jump_counted() -> None:
    tracker = FeedIntegrityTracker(large_jump_seconds=60.0)
    tracker.on_quote(_quote(_INSIDE))

    violations = tracker.on_quote(_quote(datetime(2026, 9, 24, 4, 5, tzinfo=UTC)))

    assert GapKind.LARGE_JUMP in violations
    assert tracker.snapshot().large_jumps == 1


def test_session_boundary_violation_counted_once_per_day() -> None:
    tracker = FeedIntegrityTracker()

    first = tracker.on_quote(_quote(_OUTSIDE))
    second = tracker.on_quote(_quote(datetime(2026, 9, 24, 2, 1, tzinfo=UTC)))

    assert first == (GapKind.SESSION_BOUNDARY,)
    assert second == ()
    assert tracker.snapshot().session_boundary_violations == 1


def test_mcx_tick_outside_cash_hours_is_not_a_violation() -> None:
    tracker = FeedIntegrityTracker()
    mcx = Quote(
        instrument=Equity.of("MCX", "CRUDEOIL"),
        ltp=Price(value=Decimal("100")),
        timestamp=_OUTSIDE,
    )

    assert tracker.on_quote(mcx) == ()


def test_duplicate_closed_bar_and_missing_bars() -> None:
    tracker = FeedIntegrityTracker()
    base = 1_774_000_000

    assert tracker.on_bar(_bar(base)) == ()
    assert tracker.on_bar(_bar(base)) == (GapKind.DUPLICATE_CLOSED_BAR,)
    # Two-minute jump over a 1m bucket == one missing bar — now surfaced in violations.
    assert tracker.on_bar(_bar(base + 120)) == (GapKind.MISSING_BAR,)

    snapshot = tracker.snapshot()
    assert snapshot.duplicate_closed_bars == 1
    assert snapshot.missing_bars == 1
    assert snapshot.bars_seen == 3


def test_adjacent_bars_produce_no_missing_bar_violation() -> None:
    tracker = FeedIntegrityTracker()
    base = 1_774_000_000
    # Exact 1-minute step — no gap, no MISSING_BAR violation.
    assert tracker.on_bar(_bar(base)) == ()
    assert tracker.on_bar(_bar(base + 60)) == ()


def test_forming_bar_is_ignored() -> None:
    tracker = FeedIntegrityTracker()

    assert tracker.on_bar(_bar(1_774_000_000, closed=False)) == ()
    assert tracker.snapshot().bars_seen == 0


def test_metrics_counters_incremented() -> None:
    registry = MetricsRegistry()
    tracker = FeedIntegrityTracker(metrics=registry)

    tracker.on_quote(_quote(_INSIDE))
    tracker.on_quote(_quote(_INSIDE))
    tracker.on_quote(_quote(datetime(2026, 9, 24, 4, 5, tzinfo=UTC)))

    assert registry.get("feed_duplicate_events_total") == 1
    assert registry.get("feed_large_jumps_total") == 1
    assert registry.get("feed_gaps_total") == 2


def test_reset_clears_state() -> None:
    tracker = FeedIntegrityTracker()
    tracker.on_quote(_quote(_INSIDE))
    tracker.on_quote(_quote(_INSIDE))

    tracker.reset()

    assert tracker.snapshot().total_violations == 0
    assert tracker.snapshot().events_seen == 0
    # A quote after reset is fresh again, not a duplicate of pre-reset state.
    assert tracker.on_quote(_quote(_INSIDE)) == ()
