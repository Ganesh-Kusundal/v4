"""Feed recovery path: DEGRADED → RESYNCHRONIZING → READY.

``MarketFeed.check_stale()`` marks the supervisor DEGRADED when the tape goes
quiet. Before this lane nothing wrote READY on that path, so a stale feed stayed
DEGRADED for the life of the session and every order was refused with
``feed_not_ready``. The owned ``FeedStaleMonitor`` poll now triggers the
fail-closed ``FeedRecoveryCoordinator`` on the poll *after* the one that detected
staleness: the coordinator reconciles history and writes READY, or fails closed
and writes HALTED.

The recovery trigger is deliberately narrow, and two negative controls here hold
it that way:

* it fires only on a DEGRADED cause the *quote* path owns — a fresh ``StaleFeed``
  from ``check_stale()``. A DEGRADED written by ``OrderStreamWatchdog`` (a dead
  order socket) must **not** be recovered, or the one failure the watchdog exists
  to catch would be flipped to READY one poll later;
* it fires only while the feed is ``active``. ``MarketFeed.stop()`` sets DEGRADED
  via ``disconnected()``, and recovering the now-empty feed would fail closed to
  HALTED, which is sticky.

These are integration tests with real components end to end: a real ``MarketFeed``
on a real ``PaperBroker``, a real ``ReactiveBus``, a real ``FeedSupervisor``, a
real ``OrderStreamWatchdog``, and a real ``ParquetHistoryProvider`` reading a real
``ParquetStorage`` over a temp lake. No broker transport and no history source is
faked; the only thing the tests reach for is the clock the staleness checks read.
"""

from __future__ import annotations

import time
from datetime import UTC, datetime, timedelta

import pandas as pd
from tradex_brokers.paper import PaperBroker
from tradex_domain.instruments import Equity
from tradex_market_data.parquet_storage import ParquetStorage
from tradex_reactive.bus import ReactiveBus
from tradex_runtime.feed_monitor import FeedStaleMonitor, OrderStreamWatchdog
from tradex_runtime.feed_recovery import FeedRecoveryCoordinator, ParquetHistoryProvider
from tradex_runtime.feed_supervisor import FeedState, FeedSupervisor
from tradex_runtime.market_feed import MarketFeed

_RELIANCE = Equity.of("NSE", "RELIANCE")
#: 2026-09-24T10:00Z — a Thursday session minute, the stamp the lake is seeded on.
_EVENT_AT = datetime(2026, 9, 24, 10, 0, tzinfo=UTC)


def _wait_until(predicate: object, timeout: float = 3.0) -> bool:
    """Spin until *predicate* is true; recovery is dispatched on a daemon thread."""
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if predicate():  # type: ignore[operator]
            return True
        time.sleep(0.01)
    return predicate()  # type: ignore[operator]


def _seed_minute(store: ParquetStorage, stamp: str, *, symbol: str = "RELIANCE") -> None:
    store.upsert(
        pd.DataFrame(
            [
                dict(
                    symbol=symbol,
                    exchange="NSE",
                    kind="equity",
                    timeframe="1m",
                    timestamp=stamp,
                    open=100.0,
                    high=101.0,
                    low=99.0,
                    close=100.5,
                    volume=10.0,
                )
            ]
        )
    )


def _history_provider(tmp_path) -> ParquetHistoryProvider:
    """A real provider over a real temp lake seeded with one session bar."""
    store = ParquetStorage(tmp_path)
    _seed_minute(store, "2026-09-24 10:00:00")
    return ParquetHistoryProvider(store=store)


def _ready_feed(*, stale_after: float = 0.05) -> MarketFeed:
    """A real feed on a real paper broker, subscribed and READY."""
    feed = MarketFeed(broker=PaperBroker(), bus=ReactiveBus(), stale_after=stale_after)
    feed.subscribe([_RELIANCE])
    supervisor = feed.supervisor
    supervisor.connected()
    supervisor.recovery_started()
    supervisor.recovery_succeeded(last_event_at=_EVENT_AT, recovered_through=_EVENT_AT)
    return feed


def _make_stale(feed: MarketFeed) -> None:
    feed._last_tick[_RELIANCE.instrument_id] = datetime.now(UTC) - timedelta(seconds=1)


# ---------------------------------------------------------------------------
# The recovery path
# ---------------------------------------------------------------------------


def test_degraded_feed_triggers_recovery_on_next_poll(tmp_path) -> None:
    feed = _ready_feed()
    coordinator = FeedRecoveryCoordinator(
        feed, supervisor=feed.supervisor, history_provider=_history_provider(tmp_path)
    )
    feed.set_recovery(coordinator)
    monitor = FeedStaleMonitor(feed, interval_seconds=0.05, feed_recovery=coordinator)
    _make_stale(feed)

    # Poll 1 detects staleness and demotes the feed; it does not yet recover.
    stale = monitor.poll_once()
    assert stale
    assert feed.supervisor.state is FeedState.DEGRADED

    # Poll 2 hands the quote-side DEGRADED to the coordinator on a daemon thread.
    _make_stale(feed)  # keep the tape stale across the poll boundary
    monitor.poll_once()
    assert _wait_until(lambda: feed.supervisor.state is FeedState.READY)
    assert feed.supervisor.ready is True


def test_failed_recovery_halts_feed(tmp_path) -> None:
    feed = _ready_feed()
    # Two bars five minutes apart with an allowance of zero: history itself proves
    # a hole, so the coordinator fails closed rather than seeding a blind book.
    store = ParquetStorage(tmp_path)
    _seed_minute(store, "2026-09-24 10:00:00")
    _seed_minute(store, "2026-09-24 10:05:00")
    coordinator = FeedRecoveryCoordinator(
        feed,
        supervisor=feed.supervisor,
        history_provider=ParquetHistoryProvider(store=store),
        max_missing_bars=0,
    )
    feed.set_recovery(coordinator)
    feed.set_recovery(coordinator)
    monitor = FeedStaleMonitor(feed, interval_seconds=0.05, feed_recovery=coordinator)
    _make_stale(feed)

    monitor.poll_once()
    assert feed.supervisor.state is FeedState.DEGRADED

    _make_stale(feed)  # keep the tape stale across the poll boundary
    monitor.poll_once()
    assert _wait_until(lambda: feed.supervisor.state is FeedState.HALTED)
    assert feed.supervisor.ready is False


def test_no_recovery_coordinator_leaves_degraded() -> None:
    feed = _ready_feed()
    monitor = FeedStaleMonitor(feed, interval_seconds=0.05)  # no feed_recovery
    _make_stale(feed)

    monitor.poll_once()
    assert feed.supervisor.state is FeedState.DEGRADED

    _make_stale(feed)
    monitor.poll_once()
    assert feed.supervisor.state is FeedState.DEGRADED  # unchanged, orders stay gated


# ---------------------------------------------------------------------------
# Negative control: a dead order socket must not be recovered to READY
# ---------------------------------------------------------------------------


def test_order_stream_degraded_is_not_recovered(tmp_path) -> None:
    """The Critical fail-open: an order-stream DEGRADED must stay DEGRADED.

    The order watchdog marks the same supervisor the quote path marks. Recovery
    is gated on ``check_stale()`` returning stale events, so a DEGRADED whose
    only evidence is a silent order socket is never handed to the coordinator.
    """
    feed = _ready_feed()
    coordinator = FeedRecoveryCoordinator(
        feed, supervisor=feed.supervisor, history_provider=_history_provider(tmp_path)
    )
    feed.set_recovery(coordinator)
    watchdog = OrderStreamWatchdog(feed.supervisor, stale_after=0.01)
    # A frame long ago and no frame since: the socket is answering nothing.
    watchdog.record_frame(at=datetime.now(UTC) - timedelta(seconds=30))
    monitor = FeedStaleMonitor(
        feed,
        interval_seconds=0.05,
        order_watchdog=watchdog,
        feed_recovery=coordinator,
    )
    # The quote tape is healthy: a fresh tick, so check_stale() returns nothing.
    feed._last_tick[_RELIANCE.instrument_id] = datetime.now(UTC)

    # Poll 1: the watchdog is what demotes the feed.
    assert monitor.poll_once() == []
    assert feed.supervisor.state is FeedState.DEGRADED

    # Poll 2: state is DEGRADED but this poll produced no StaleFeed — no recovery.
    assert monitor.poll_once() == []
    assert feed.supervisor.state is FeedState.DEGRADED
    assert feed.supervisor.ready is False

    # Once the order socket answers again, a real quote staleness opens the path
    # back to READY: the gate is not permanently stuck, only fail-closed while
    # the order stream is provably dead.
    watchdog.record_frame()
    _make_stale(feed)
    monitor.poll_once()
    assert _wait_until(lambda: feed.supervisor.state is FeedState.READY)


def test_order_dead_with_stale_tape_is_not_recovered(tmp_path) -> None:
    """Co-occurring causes: a stale tape must not paper over a dead order socket.

    With both a fresh ``StaleFeed`` and a silent order stream, the quote-side
    evidence alone would authorize recovery and write READY while the order
    socket is still dead. The watchdog's one-shot latch would not re-degrade it,
    so the gate refuses whenever the order stream is provably stale.
    """
    feed = _ready_feed()
    coordinator = FeedRecoveryCoordinator(
        feed, supervisor=feed.supervisor, history_provider=_history_provider(tmp_path)
    )
    feed.set_recovery(coordinator)
    watchdog = OrderStreamWatchdog(feed.supervisor, stale_after=0.01)
    watchdog.record_frame(at=datetime.now(UTC) - timedelta(seconds=30))
    monitor = FeedStaleMonitor(
        feed,
        interval_seconds=0.05,
        order_watchdog=watchdog,
        feed_recovery=coordinator,
    )

    _make_stale(feed)  # quote tape stale AND order socket dead
    monitor.poll_once()
    _make_stale(feed)
    monitor.poll_once()
    time.sleep(0.1)  # give any (wrong) recovery time to land

    assert feed.supervisor.state is FeedState.DEGRADED
    assert feed.supervisor.ready is False


# ---------------------------------------------------------------------------
# Negative control: a deliberate stop must not be recovered into HALTED
# ---------------------------------------------------------------------------


def test_stopped_feed_is_not_recovered_into_halted(tmp_path) -> None:
    """After ``stop()`` the feed is DEGRADED and empty; recovery would HALT it.

    ``MarketFeed.stop()`` clears the wanted set and calls ``disconnected()``.
    Recovering the empty feed fails closed to HALTED, and HALTED is sticky, so a
    teardown would leave a session that can never trade again.
    """
    feed = _ready_feed()
    coordinator = FeedRecoveryCoordinator(
        feed, supervisor=feed.supervisor, history_provider=_history_provider(tmp_path)
    )
    feed.set_recovery(coordinator)
    monitor = FeedStaleMonitor(feed, interval_seconds=0.05, feed_recovery=coordinator)

    feed.stop()
    assert feed.active is False
    assert feed.supervisor.state is FeedState.DEGRADED

    monitor.poll_once()
    monitor.poll_once()
    time.sleep(0.1)  # give any (wrong) daemon recovery time to land
    assert feed.supervisor.state is FeedState.DEGRADED


# ---------------------------------------------------------------------------
# The in-flight guard is shared with the feed's own recovery path
# ---------------------------------------------------------------------------


def test_request_recovery_returns_false_while_one_is_in_flight(tmp_path) -> None:
    """A second recovery request while one runs is refused, not queued.

    ``request_recovery()`` delegates to ``MarketFeed._trigger_recovery``, so the
    poll trigger and the reconnect/integrity triggers share one in-flight flag.
    The slot is claimed here the way the reconnect path claims it — through the
    feed's own guard — with no stub standing in for any component.
    """
    feed = _ready_feed()
    coordinator = FeedRecoveryCoordinator(
        feed, supervisor=feed.supervisor, history_provider=_history_provider(tmp_path)
    )
    feed.set_recovery(coordinator)

    claimed = feed._claim_recovery()  # an in-flight recovery, as reconnect holds it
    assert claimed is coordinator
    try:
        assert feed.request_recovery() is False  # refused: one is already running
    finally:
        feed._release_recovery()

    assert feed.request_recovery() is True  # slot free again
    assert _wait_until(lambda: feed._recovery_in_flight is False)


def test_monitor_trigger_is_refused_while_the_feed_guard_is_held(tmp_path) -> None:
    """The poll trigger cannot start a second recovery while one is in flight.

    This is what routing the monitor through the feed's own trigger buys: if the
    monitor called ``coordinator.recover()`` directly (the bypass the review
    flagged), it would recover even with the guard held. Held here through the
    feed's real ``_claim_recovery`` — the same door a socket reconnect uses.
    """
    feed = _ready_feed()
    coordinator = FeedRecoveryCoordinator(
        feed, supervisor=feed.supervisor, history_provider=_history_provider(tmp_path)
    )
    feed.set_recovery(coordinator)
    monitor = FeedStaleMonitor(feed, interval_seconds=0.05, feed_recovery=coordinator)

    assert feed._claim_recovery() is coordinator  # reconnect holds the slot
    feed.supervisor.mark_stale()  # …and the quote tape goes quiet meanwhile
    _make_stale(feed)

    monitor.poll_once()
    time.sleep(0.1)  # give any (wrong) recovery time to land
    assert feed.supervisor.state is FeedState.DEGRADED, (
        "the monitor must not recover while a recovery is already in flight"
    )

    feed._release_recovery()
    _make_stale(feed)
    monitor.poll_once()
    assert _wait_until(lambda: feed.supervisor.state is FeedState.READY)


# ---------------------------------------------------------------------------
# The poll trigger is wired on the production path
# ---------------------------------------------------------------------------


def test_live_compose_wires_the_coordinator_into_the_monitor(tmp_path) -> None:
    """``_compose_live_feed`` constructs the monitor and the coordinator.

    If the monitor is not handed the coordinator, this whole lane is dead code in
    the one mode that needs it — a stale live feed would stay DEGRADED forever.
    Asserted through the real composition function with a real ``PaperBroker``,
    the same wiring the live server uses.
    """
    from tradex_config.schema import AppConfig
    from tradex_domain import BrokerId
    from tradex_runtime.metrics import MetricsRegistry
    from tradex_runtime.startup import _compose_live_feed

    supervisor = FeedSupervisor()
    bundle = _compose_live_feed(
        cfg=AppConfig(broker_id=BrokerId.DHAN, mode="live", live_enabled=True),
        broker=PaperBroker(),
        bus=ReactiveBus(),
        feed_supervisor=supervisor,
        metrics=MetricsRegistry(),
        order_watchdog=OrderStreamWatchdog(supervisor),
    )

    assert bundle.feed_recovery is not None
    assert bundle.feed_monitor._feed_recovery is bundle.feed_recovery
