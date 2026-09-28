"""AC-12: the order/portfolio stream gets its own frame-age watchdog.

The quote tape already has one (``FeedStaleMonitor`` -> ``MarketFeed.
check_stale``). The order socket had none, and it is the one whose silence is
most expensive: a dead order stream blocks in ``recv()`` forever, nothing
raises, and a resting order is still reported live. These tests pin the whole
``record_frame`` -> ``check`` -> ``DEGRADED`` path, in **both** directions:

* a stream that goes quiet past the threshold must DEGRADE (missed DEGRADED =
  a resting order with no exit), and
* a healthy stream, a not-yet-crossed threshold, a disabled watchdog and a
  never-connected stream must all stay READY (false DEGRADED = a refusal to
  trade).
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

from tradex_runtime.feed_monitor import (
    ORDER_FEED_STALE_AFTER,
    OrderStreamWatchdog,
)
from tradex_runtime.feed_supervisor import FeedState, FeedSupervisor


class _Clock:
    """Manually advanced clock so the thresholds are exact, not slept through."""

    def __init__(self, start: datetime | None = None) -> None:
        self.now = start or datetime(2026, 9, 24, 10, 0, tzinfo=UTC)

    def __call__(self) -> datetime:
        return self.now

    def advance(self, seconds: float) -> None:
        self.now = self.now + timedelta(seconds=seconds)


def _ready_supervisor() -> FeedSupervisor:
    supervisor = FeedSupervisor()
    supervisor.connected()
    supervisor.recovery_started()
    supervisor.recovery_succeeded(
        last_event_at=datetime.now(UTC), recovered_through=datetime.now(UTC)
    )
    assert supervisor.state is FeedState.READY
    return supervisor


# -- the threshold the spec fixes ------------------------------------------


def test_order_threshold_is_sixty_seconds() -> None:
    """AC-12: 30s on quotes, 60s on the order stream."""
    assert ORDER_FEED_STALE_AFTER == 60.0
    assert OrderStreamWatchdog(_ready_supervisor()).stale_after == 60.0


# -- the point of the lane --------------------------------------------------


def test_silent_order_stream_degrades_after_sixty_seconds() -> None:
    supervisor = _ready_supervisor()
    clock = _Clock()
    watchdog = OrderStreamWatchdog(supervisor, clock=clock)

    watchdog.record_frame()          # the socket answered once
    clock.advance(61.0)              # ... then went silent past the window

    assert watchdog.check() is True
    assert supervisor.state is FeedState.DEGRADED


def test_order_stream_goes_degraded_even_with_no_fills() -> None:
    """A DEGRADED verdict is about the socket, not about order activity.

    This is the failure the spec names: the stream is silent *because* it is
    dead, and "we saw no fills" is exactly what a dead socket looks like from
    the outside. Nothing is booked, nothing raises, and the resting order
    stays believed live unless the frames themselves are timed out.
    """
    supervisor = _ready_supervisor()
    clock = _Clock()
    watchdog = OrderStreamWatchdog(supervisor, clock=clock)
    watchdog.record_frame()
    clock.advance(90.0)

    watchdog.check()

    assert supervisor.state is FeedState.DEGRADED
    assert supervisor.ready is False


# -- negative controls: none of these may degrade ---------------------------


def test_healthy_order_stream_stays_ready() -> None:
    supervisor = _ready_supervisor()
    clock = _Clock()
    watchdog = OrderStreamWatchdog(supervisor, clock=clock)

    for _ in range(5):
        watchdog.record_frame()
        clock.advance(10.0)
        assert watchdog.check() is False

    assert supervisor.state is FeedState.READY


def test_threshold_not_crossed_does_not_degrade() -> None:
    """59s of silence is inside the window; the boundary is strict >."""
    supervisor = _ready_supervisor()
    clock = _Clock()
    watchdog = OrderStreamWatchdog(supervisor, clock=clock)
    watchdog.record_frame()

    clock.advance(59.0)
    assert watchdog.check() is False
    assert supervisor.state is FeedState.READY

    clock.advance(2.0)              # now past 60
    assert watchdog.check() is True
    assert supervisor.state is FeedState.DEGRADED


def test_disabled_watchdog_never_degrades() -> None:
    """The kill switch must actually disable it, not merely shorten it."""
    supervisor = _ready_supervisor()
    clock = _Clock()
    watchdog = OrderStreamWatchdog(supervisor, enabled=False, clock=clock)

    watchdog.record_frame()
    clock.advance(10_000.0)

    assert watchdog.enabled is False
    assert watchdog.check() is False
    assert supervisor.state is FeedState.READY


def test_stream_that_never_delivered_a_frame_is_not_aged() -> None:
    """A boot-time subscription may legitimately carry no updates.

    Ageing a stream that was never up would mark every quiet session
    DEGRADED on open, which is the false-positive direction this lane has to
    avoid. Only silence *after* a live stream is a fault.
    """
    supervisor = _ready_supervisor()
    clock = _Clock()
    watchdog = OrderStreamWatchdog(supervisor, clock=clock)

    clock.advance(10_000.0)

    assert watchdog.age_seconds() is None
    assert watchdog.check() is False
    assert supervisor.state is FeedState.READY


# -- reporting behaviour ----------------------------------------------------


def test_degrade_is_reported_once_per_outage() -> None:
    """Polling every second must not spam a DEGRADED verdict.

    The monitor polls on a short interval, so a naive check would re-log the
    same outage on every tick. The verdict fires once; a subsequent frame
    re-arms it.
    """
    supervisor = _ready_supervisor()
    clock = _Clock()
    watchdog = OrderStreamWatchdog(supervisor, clock=clock)
    watchdog.record_frame()
    clock.advance(61.0)

    assert watchdog.check() is True
    assert watchdog.check() is False
    assert watchdog.check() is False
    assert supervisor.state is FeedState.DEGRADED

    watchdog.record_frame()          # socket answers again
    assert watchdog.check() is False
    clock.advance(61.0)
    assert watchdog.check() is True   # re-armed for a second outage


def test_check_survives_a_supervisor_without_mark_stale() -> None:
    """A monitoring fault must not raise into the poll loop."""

    class _Bare:
        pass

    watchdog = OrderStreamWatchdog(_Bare(), clock=_Clock())
    watchdog.record_frame()

    assert watchdog.check() is False


def test_reset_forgets_the_frame_clock() -> None:
    """After a deliberate reconnect the old frame must not age the new socket."""
    supervisor = _ready_supervisor()
    clock = _Clock()
    watchdog = OrderStreamWatchdog(supervisor, clock=clock)
    watchdog.record_frame()

    watchdog.reset()

    assert watchdog.last_frame_at is None
    clock.advance(10_000.0)
    assert watchdog.check() is False
    assert supervisor.state is FeedState.READY


def test_halted_feed_is_not_downgraded_by_the_watchdog() -> None:
    """HALTED is terminal; the watchdog must not rewrite it to DEGRADED."""
    supervisor = _ready_supervisor()
    clock = _Clock()
    watchdog = OrderStreamWatchdog(supervisor, clock=clock)
    watchdog.record_frame()
    supervisor.recovery_failed("history gap")
    clock.advance(61.0)

    watchdog.check()

    assert supervisor.state is FeedState.HALTED


# -- wiring: the watchdog is actually reached by real order frames ----------


def _order(status: str = "NEW") -> object:
    """A real domain Order, built the way the venue stream delivers one."""
    from decimal import Decimal

    from tradex_domain import OrderSide, OrderStatus, OrderType, TimeInForce
    from tradex_domain.execution import Order
    from tradex_domain.instruments import Equity
    from tradex_domain.value_objects import CorrelationId, OrderId, Price, Quantity

    return Order(
        order_id=OrderId(value="dhan-1"),
        instrument=Equity.of("NSE", "RELIANCE"),
        side=OrderSide.BUY,
        order_type=OrderType.MARKET,
        quantity=Quantity(value=Decimal("10")),
        price=Price(value=Decimal("100")),
        time_in_force=TimeInForce.DAY,
        status=OrderStatus(status),
        correlation_id=CorrelationId(value="c1"),
        filled_quantity=Quantity(value=Decimal("0")),
    )


class _Stream:
    """Minimal order stream: captures the handler the bridge registers."""

    def __init__(self) -> None:
        self.handler = None

    def subscribe_orders(self, handler):
        self.handler = handler
        return object()

    def emit(self, order) -> None:
        assert self.handler is not None
        self.handler(order)


def test_fill_bridge_stamps_the_watchdog_on_every_frame() -> None:
    """The frame clock must be driven by the order stream itself.

    Unit tests on the watchdog would pass even if nothing ever called
    ``record_frame``; this closes that gap by driving a real broker order
    update through ``LiveFillBridge`` and asserting the clock moved.
    """
    from tradex_runtime.live_fill_bridge import LiveFillBridge

    stream = _Stream()
    clock = _Clock()
    watchdog = OrderStreamWatchdog(_ready_supervisor(), clock=clock)

    # A bus with no ``of_type`` skips the correlation-index wiring, which is
    # not what this test is about.
    LiveFillBridge(
        object(), object(), stream.subscribe_orders, on_frame=watchdog.record_frame
    )
    assert watchdog.last_frame_at is None

    stream.emit(_order())

    # A status-only update the bridge discards still counts as liveness: the
    # question is whether the socket answered, and it did.
    assert watchdog.last_frame_at == clock.now


def test_fill_bridge_survives_a_raising_liveness_hook() -> None:
    """A broken liveness hook must not cost us the order update.

    Stamping liveness is a side effect; letting it raise would turn a
    monitoring bug into a dropped frame on the money path.
    """
    from tradex_runtime.live_fill_bridge import LiveFillBridge

    def _boom() -> None:
        raise RuntimeError("liveness hook exploded")

    stream = _Stream()
    LiveFillBridge(object(), object(), stream.subscribe_orders, on_frame=_boom)

    stream.emit(_order())  # must not raise


def test_order_watchdog_is_driven_by_the_owned_poll_loop() -> None:
    """End to end: the session's monitor thread checks the order stream too.

    Wiring the watchdog into ``FeedStaleMonitor`` is what stops it from being
    a class nobody polls. This drives a real poll and asserts a quiet order
    stream degrades the feed without any strategy or operator involvement.
    """
    import time

    from tradex_reactive.bus import ReactiveBus
    from tradex_runtime.feed_monitor import FeedStaleMonitor
    from tradex_runtime.market_feed import MarketFeed

    class _Broker:
        def subscribe_quotes(self, instruments, handler):
            return object()

        def unsubscribe(self, subscription):
            return None

        def unsubscribe_instruments(self, instruments):
            return None

    supervisor = _ready_supervisor()
    feed = MarketFeed(broker=_Broker(), bus=ReactiveBus(), stale_after=30.0)
    # Real clock here: the point is the background thread ageing the frame in
    # real time between two polls, not an injected clock.
    watchdog = OrderStreamWatchdog(supervisor, stale_after=0.05)
    watchdog.record_frame()

    monitor = FeedStaleMonitor(
        feed, interval_seconds=0.01, order_watchdog=watchdog
    )
    monitor.start()
    try:
        deadline = time.monotonic() + 2.0
        while time.monotonic() < deadline:
            if supervisor.state is FeedState.DEGRADED:
                break
            time.sleep(0.01)
    finally:
        monitor.stop()

    assert supervisor.state is FeedState.DEGRADED
