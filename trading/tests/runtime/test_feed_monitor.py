"""Tests for the owned stale-feed monitor (runtime.feed_monitor)."""

from __future__ import annotations

import time
from datetime import UTC, datetime, timedelta

from tradex_domain.instruments import Equity, Instrument

from tradex_trading.reactive.bus import ReactiveBus
from tradex_trading.runtime.feed_monitor import FeedStaleMonitor
from tradex_trading.runtime.feed_supervisor import FeedState
from tradex_trading.runtime.market_feed import MarketFeed
from tradex_trading.runtime.metrics import MetricsRegistry


class _FakeBroker:
    """Minimal broker shape accepted by ``MarketFeed.subscribe``."""

    def subscribe_quotes(self, instruments: object, handler: object) -> object:
        return object()

    def unsubscribe(self, subscription: object) -> None:
        return None

    def unsubscribe_instruments(self, instruments: object) -> None:
        return None


def _reliance() -> Instrument:
    return Equity.of("NSE", "RELIANCE")


def _stale_feed(*, stale_after: float = 30.0) -> MarketFeed:
    feed = MarketFeed(broker=_FakeBroker(), bus=ReactiveBus(), stale_after=stale_after)
    feed.start([_reliance()])
    return feed


def _make_stale(feed: MarketFeed) -> None:
    feed._last_tick[_reliance().instrument_id] = datetime.now(UTC) - timedelta(seconds=1)
    feed.supervisor.recovery_succeeded(
        last_event_at=datetime.now(UTC),
        recovered_through=datetime.now(UTC),
    )


def _wait_until(predicate, timeout: float = 2.0) -> bool:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if predicate():
            return True
        time.sleep(0.01)
    return predicate()


def test_poll_once_marks_a_stale_ready_feed_degraded() -> None:
    feed = _stale_feed(stale_after=0.05)
    monitor = FeedStaleMonitor(feed, interval_seconds=0.05)
    _make_stale(feed)
    assert feed.supervisor.state is FeedState.READY

    stale = monitor.poll_once()

    assert len(stale) == 1
    assert feed.supervisor.state is FeedState.DEGRADED
    assert feed.supervisor.ready is False


def test_poll_once_noops_when_no_instrument_is_wanted() -> None:
    feed = MarketFeed(broker=_FakeBroker(), bus=ReactiveBus(), stale_after=0.01)
    monitor = FeedStaleMonitor(feed, interval_seconds=0.01)

    assert monitor.poll_once() == []


def test_poll_once_never_raises_when_check_stale_fails() -> None:
    class _Exploding:
        active = True
        stale_after = 1.0
        supervisor = None

        def check_stale(self) -> list[object]:
            raise RuntimeError("broker hiccup")

    monitor = FeedStaleMonitor(_Exploding(), interval_seconds=0.05)

    assert monitor.poll_once() == []


def test_metrics_gauges_are_published() -> None:
    registry = MetricsRegistry()
    feed = _stale_feed(stale_after=0.05)
    monitor = FeedStaleMonitor(feed, interval_seconds=0.05, metrics=registry)
    _make_stale(feed)

    monitor.poll_once()

    assert registry.get("feed_state") == 4.0  # degraded
    assert registry.get("feed_generation") == 1.0
    assert registry.get("feed_age_seconds") >= 0.0


def test_status_snapshot_shape() -> None:
    feed = _stale_feed()
    monitor = FeedStaleMonitor(feed, interval_seconds=0.05)

    status = monitor.status()

    assert status["state"] == "resynchronizing"
    assert status["ready"] is False
    assert status["generation"] == 1
    assert status["last_event_at"] is None
    assert status["age_seconds"] is None


def test_status_includes_age_after_a_tick() -> None:
    feed = _stale_feed()
    stamp = datetime.now(UTC) - timedelta(seconds=5)
    feed.supervisor.record_event(stamp)
    monitor = FeedStaleMonitor(feed, interval_seconds=0.05)

    status = monitor.status()

    assert status["last_event_at"] == stamp.isoformat()
    assert status["age_seconds"] >= 5.0


def test_default_interval_is_half_the_staleness_threshold() -> None:
    feed = _stale_feed(stale_after=10.0)

    assert FeedStaleMonitor(feed).interval_seconds == 5.0


def test_rejects_non_positive_interval() -> None:
    import pytest

    with pytest.raises(ValueError):
        FeedStaleMonitor(_stale_feed(), interval_seconds=0)


def test_thread_starts_once_and_stops_idempotently() -> None:
    monitor = FeedStaleMonitor(_stale_feed(), interval_seconds=0.01)

    monitor.start()
    first_thread = monitor._thread
    monitor.start()

    assert monitor.running is True
    assert monitor._thread is first_thread  # no duplicate timer

    monitor.stop()
    assert monitor.running is False
    monitor.stop()  # idempotent


def test_background_loop_degrades_a_stale_feed() -> None:
    feed = _stale_feed(stale_after=0.05)
    _make_stale(feed)
    monitor = FeedStaleMonitor(feed, interval_seconds=0.01)

    monitor.start()
    try:
        assert _wait_until(lambda: feed.supervisor.state is FeedState.DEGRADED)
    finally:
        monitor.stop()

    assert monitor.running is False


def test_context_manager_starts_and_stops() -> None:
    monitor = FeedStaleMonitor(_stale_feed(), interval_seconds=0.01)

    with monitor:
        assert monitor.running is True

    assert monitor.running is False
