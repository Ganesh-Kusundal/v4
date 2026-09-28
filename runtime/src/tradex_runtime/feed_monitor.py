"""Automatic feed stale monitoring.

``MarketFeed.check_stale()`` is poll-driven by design (it must be callable from
any thread without owning a timer). Relying on an optional caller to remember
to poll it means a stale feed can go unnoticed — the exact failure that lets a
stale price drive a live order. :class:`FeedStaleMonitor` makes the poll an
owned lifecycle: the trading session starts one bounded daemon thread when the
session goes live and stops it on ``stop()`` (no duplicate timers).

The monitor also exports the feed's health as metrics, so operators can alert
on ``feed_state``/``feed_age_seconds`` without a second polling path.
"""

from __future__ import annotations

import logging
import threading
from collections.abc import Callable
from datetime import UTC, datetime
from typing import Any

log = logging.getLogger(__name__)

#: Numeric encoding of ``FeedState`` for the ``feed_state`` gauge.
_STATE_CODES: dict[str, float] = {
    "new": 0.0,
    "connecting": 1.0,
    "resynchronizing": 2.0,
    "ready": 3.0,
    "degraded": 4.0,
    "halted": 5.0,
}


def _default_interval(feed: Any) -> float:
    """Poll fast enough to catch staleness shortly after it happens."""
    stale_after = float(getattr(feed, "stale_after", 30.0) or 30.0)
    return max(0.5, min(stale_after / 2.0, 30.0))


class FeedStaleMonitor:
    """Owns the periodic ``check_stale`` poll for one market feed.

    Parameters
    ----------
    feed:
        ``MarketFeed`` (or any object exposing ``check_stale``, ``supervisor``,
        ``active`` and ``stale_after``).
    interval_seconds:
        Poll period. Defaults to half the feed's staleness threshold (bounded
        to 0.5-30s) so a stale feed is flagged within one threshold.
    metrics:
        Optional ``MetricsRegistry``; gauges ``feed_age_seconds``,
        ``feed_state``, ``feed_generation`` are set on every poll.
    on_status:
        Optional callback invoked with the status dict whenever it changes
        (state, ready, generation, last_event_at, age_seconds). Used by the
        WebSocket bridge to push ``feed_status`` to clients.
    """

    def __init__(
        self,
        feed: Any,
        *,
        interval_seconds: float | None = None,
        metrics: Any | None = None,
        on_status: Callable[[dict[str, Any]], None] | None = None,
        clock: Callable[[], datetime] | None = None,
        order_watchdog: OrderStreamWatchdog | None = None,
    ) -> None:
        self._feed = feed
        # The order stream is checked by this same owned thread rather than a
        # second one: one timer means one lifecycle, one stop path, and no
        # way for a poll to outlive the session that started it.
        self._order_watchdog = order_watchdog
        self._interval = float(
            interval_seconds if interval_seconds is not None else _default_interval(feed)
        )
        if self._interval <= 0:
            raise ValueError("interval_seconds must be positive")
        self._metrics = metrics
        self._on_status = on_status
        self._clock = clock or (lambda: datetime.now(UTC))
        self._thread: threading.Thread | None = None
        self._stop = threading.Event()
        self._last_status: dict[str, Any] | None = None
        self._lock = threading.Lock()

    # -- lifecycle ----------------------------------------------------------

    @property
    def running(self) -> bool:
        """Whether the polling thread is currently alive."""
        thread = self._thread
        return thread is not None and thread.is_alive()

    @property
    def interval_seconds(self) -> float:
        """The configured poll period."""
        return self._interval

    def start(self) -> None:
        """Start the poll loop. Idempotent — never spawns a second timer."""
        with self._lock:
            if self.running:
                return
            self._stop.clear()
            thread = threading.Thread(
                target=self._run,
                name="feed-stale-monitor",
                daemon=True,
            )
            self._thread = thread
            thread.start()

    def stop(self, timeout_seconds: float = 5.0) -> None:
        """Signal the poll loop to exit and join it. Idempotent."""
        with self._lock:
            thread = self._thread
            self._stop.set()
        if thread is not None and thread.is_alive():
            thread.join(timeout=timeout_seconds)
            if thread.is_alive():  # pragma: no cover - defensive
                log.warning("feed stale monitor did not stop within %.1fs", timeout_seconds)
        with self._lock:
            self._thread = None

    def __enter__(self) -> FeedStaleMonitor:
        self.start()
        return self

    def __exit__(self, *args: object) -> None:
        self.stop()

    # -- polling ------------------------------------------------------------

    def poll_once(self) -> list[Any]:
        """Run one bounded check; return the ``StaleFeed`` events emitted.

        Safe on any thread. Never raises: a broker-side failure while checking
        staleness must not kill the monitor (the feed stays DEGRADED, which is
        the safe direction).
        """
        stale: list[Any] = []
        feed = self._feed
        if feed is not None and bool(getattr(feed, "active", False)):
            check = getattr(feed, "check_stale", None)
            if callable(check):
                try:
                    stale = list(check())
                except Exception:  # noqa: BLE001 - monitoring must not crash
                    log.warning("feed check_stale failed", exc_info=True)
        watchdog = self._order_watchdog
        if watchdog is not None:
            try:
                watchdog.check()
            except Exception:  # noqa: BLE001 - monitoring must not crash
                log.warning("order stream watchdog check failed", exc_info=True)
        self._publish_metrics()
        self._notify_status()
        return stale

    def status(self) -> dict[str, Any]:
        """Snapshot of feed health for the ``feed_status`` WS message."""
        feed = self._feed
        supervisor = getattr(feed, "supervisor", None) if feed is not None else None
        if supervisor is None:
            return {
                "state": None,
                "ready": None,
                "generation": None,
                "last_event_at": None,
                "age_seconds": None,
            }
        last_event_at = getattr(supervisor, "last_event_at", None)
        age: float | None = None
        if isinstance(last_event_at, datetime):
            age = max(0.0, (self._clock() - last_event_at).total_seconds())
        return {
            "state": str(getattr(supervisor, "state", "")) or None,
            "ready": bool(getattr(supervisor, "ready", False)),
            "generation": int(getattr(supervisor, "generation", 0) or 0),
            "last_event_at": (
                last_event_at.isoformat()
                if isinstance(last_event_at, datetime)
                else None
            ),
            "age_seconds": age,
        }

    # -- internals ----------------------------------------------------------

    def _run(self) -> None:
        while not self._stop.wait(self._interval):
            try:
                self.poll_once()
            except Exception:  # pragma: no cover - defensive belt
                log.exception("feed stale monitor poll failed")

    def _publish_metrics(self) -> None:
        if self._metrics is None:
            return
        snapshot = self.status()
        age = snapshot["age_seconds"]
        state = snapshot["state"]
        self._metrics.gauge("feed_age_seconds").set(
            float(age) if age is not None else 0.0
        )
        self._metrics.gauge("feed_state").set(_STATE_CODES.get(str(state), -1.0))
        self._metrics.gauge("feed_generation").set(float(snapshot["generation"] or 0))

    def _notify_status(self) -> None:
        if self._on_status is None:
            return
        snapshot = self.status()
        key = (snapshot["state"], snapshot["ready"], snapshot["generation"])
        previous = self._last_status
        if previous is not None and previous.get("_key") == key:
            return
        snapshot["_key"] = key
        self._last_status = snapshot
        try:
            self._on_status(
                {k: v for k, v in snapshot.items() if not k.startswith("_")}
            )
        except Exception:  # noqa: BLE001 - observer failures are non-fatal
            log.warning("feed status observer failed", exc_info=True)


#: Frame age (seconds) after which a quiet **order** stream is DEGRADED.
#: The quote threshold is 30s (AC-12); the order stream is twice as tolerant
#: because order updates are event-driven and legitimately rare, but it must
#: still be bounded — a socket that goes permanently quiet leaves a resting
#: order believed live, which is the more expensive failure of the two.
ORDER_FEED_STALE_AFTER = 60.0


class OrderStreamWatchdog:
    """Frame-age watchdog for the broker order/portfolio stream (AC-12).

    :class:`FeedStaleMonitor` covers the *quote* tape only. This covers the
    other socket, and it exists for one specific reason: a silently dead order
    stream is the one that leaves a resting order believed live. The socket
    blocks in ``recv()`` forever, nothing raises, and every downstream
    "no update" reading is indistinguishable from "no fills happened" — so
    the only way to notice is to time the frames out ourselves.

    Unlike the quote path this is a plain frame counter, not per-instrument:
    the order stream carries no symbol to attribute staleness to.

    It marks the **same** :class:`~tradex_runtime.feed_supervisor.FeedSupervisor`
    the quote path marks, so a dead order socket trips the existing
    ``require_feed`` gate rather than needing a second, parallel gate that
    nothing reads. That is the fail-closed direction: a false DEGRADED is a
    refusal to trade, a missed DEGRADED is a resting order with no exit.
    """

    def __init__(
        self,
        supervisor: Any,
        *,
        stale_after: float = ORDER_FEED_STALE_AFTER,
        enabled: bool = True,
        clock: Callable[[], datetime] | None = None,
    ) -> None:
        self._supervisor = supervisor
        self._stale_after = float(stale_after)
        self._enabled = bool(enabled)
        self._clock = clock or (lambda: datetime.now(UTC))
        self._last_frame_at: datetime | None = None
        self._lock = threading.Lock()
        #: Set once the stream has gone quiet, cleared by the next frame, so a
        #: DEGRADED→READY transition is a real event rather than a poll artefact.
        self._reported = False

    @property
    def enabled(self) -> bool:
        return self._enabled

    @property
    def stale_after(self) -> float:
        return self._stale_after

    @property
    def last_frame_at(self) -> datetime | None:
        with self._lock:
            return self._last_frame_at

    def record_frame(self, at: datetime | None = None) -> None:
        """Stamp liveness. Called for **every** order-stream frame.

        Deliberately records on any frame, including a status-only update the
        fill bridge later discards: the question this answers is "is the
        socket answering at all", and a status frame is an answer.
        """
        with self._lock:
            self._last_frame_at = at or self._clock()
            self._reported = False

    def reset(self) -> None:
        """Forget the frame clock — e.g. after a deliberate reconnect."""
        with self._lock:
            self._last_frame_at = None
            self._reported = False

    def age_seconds(self) -> float | None:
        """Seconds since the last frame; ``None`` if no frame has arrived."""
        with self._lock:
            last = self._last_frame_at
        if last is None:
            return None
        return max(0.0, (self._clock() - last).total_seconds())

    def check(self) -> bool:
        """Mark the feed DEGRADED if the order stream is too quiet.

        Returns ``True`` when this call is the one that degraded. Never
        raises: a watchdog that dies is a watchdog that stopped watching.

        A stream that has **never** delivered a frame is not aged — it is
        started-at-boot and may legitimately carry no updates. It is the
        *silence after* a live stream that is the failure mode, and ageing a
        stream that was never up would mark every quiet session DEGRADED.
        """
        if not self._enabled:
            return False
        age = self.age_seconds()
        if age is None or age <= self._stale_after:
            return False
        with self._lock:
            if self._reported:
                return False
            self._reported = True
        mark = getattr(self._supervisor, "mark_stale", None)
        if not callable(mark):
            return False
        mark()
        log.warning(
            "order stream silent for %.1fs (> %.1fs): marking feed DEGRADED — "
            "a resting order cannot be believed live on a dead socket",
            age, self._stale_after,
        )
        return True


__all__ = ["FeedStaleMonitor", "ORDER_FEED_STALE_AFTER", "OrderStreamWatchdog"]
