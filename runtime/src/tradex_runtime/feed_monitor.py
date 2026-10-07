"""Automatic feed stale monitoring.

``MarketFeed.check_stale()`` is poll-driven by design (it must be callable from
any thread without owning a timer). Relying on an optional caller to remember
to poll it means a stale feed can go unnoticed — the exact failure that lets a
stale price drive a live order. :class:`FeedStaleMonitor` makes the poll an
owned lifecycle: the trading session starts one bounded daemon thread when the
session goes live and stops it on ``stop()`` (no duplicate timers).

The monitor also exports the feed's health as metrics, so operators can alert
on ``feed_state``/``feed_age_seconds`` without a second polling path.

Two more liveness questions hang off this one owned thread rather than each
getting a timer of its own:

* :class:`OrderStreamWatchdog` — is the *other* socket answering at all.
* :class:`SymbolCadenceMonitor` — is *each* subscribed symbol still ticking.

That last one exists because the 30 s feed-level threshold cannot answer it.
``MarketFeed.check_stale`` only sees instruments that have ticked at least once
(``_last_tick`` is written by ``_on_quote``), so a symbol the wire stops
delivering for entirely is invisible to it, and no wire format here carries a
sequence number, so a dropped frame is indistinguishable from an illiquid
minute. One poll, three questions, one lifecycle.
"""

from __future__ import annotations

import logging
import threading
from collections.abc import Callable, Iterable
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from datetime import time as dt_time
from typing import Any

from tradex_domain.constants import CADENCE_WINDOW, ORDER_FEED_STALE_AFTER
from tradex_domain.instrument_lifecycle import (
    EXEMPT_STATES,
    normalize_status,
    row_status,
)
from tradex_domain.market_calendar import IST, MARKET_CLOSE, MARKET_OPEN
from tradex_domain.timeframe import bucket_start

from tradex_runtime.feed_supervisor import FeedState

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
        cadence_monitor: SymbolCadenceMonitor | None = None,
        feed_recovery: Any | None = None,
    ) -> None:
        self._feed = feed
        # The order stream is checked by this same owned thread rather than a
        # second one: one timer means one lifecycle, one stop path, and no
        # way for a poll to outlive the session that started it.
        self._order_watchdog = order_watchdog
        # Per-symbol cadence rides the same poll. It is a *different* question
        # (each symbol, not the tape) at a *different* timescale (3m, not 30s),
        # but the timer, the stop path and the failure semantics are identical,
        # so a second thread would buy nothing and could disagree with this one.
        self._cadence_monitor = cadence_monitor
        #: Optional fail-closed recovery coordinator (``FeedRecoveryCoordinator``).
        #: When wired, a DEGRADED feed is handed to it on the poll after the one
        #: that detected staleness: recovery reconciles history and writes READY,
        #: or fails closed and writes HALTED. Without one, a stale feed stays
        #: DEGRADED and the order gate stays shut — the safe direction, and the
        #: backward-compatible default.
        self._feed_recovery = feed_recovery
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

        Recovery is driven here too, and on the poll *after* the one that
        demoted the feed. The supervisor's state is read before this poll's
        staleness check, so a feed this poll just marked DEGRADED is not also
        recovered in the same tick: detection and recovery are two observable
        steps, and the DEGRADED window is real rather than instantaneous.
        Reading the state (rather than requiring a fresh ``StaleFeed``) is also
        what recovers a feed whose ticks resumed on their own but whose
        supervisor was never returned to READY — otherwise that is a permanent
        DEGRADED with a healthy tape and no path out.
        """
        stale: list[Any] = []
        feed = self._feed
        supervisor = getattr(feed, "supervisor", None) if feed is not None else None
        was_degraded = (
            supervisor is not None
            and getattr(supervisor, "state", None) is FeedState.DEGRADED
        )
        if feed is not None and bool(getattr(feed, "active", False)):
            check = getattr(feed, "check_stale", None)
            if callable(check):
                try:
                    stale = list(check())
                except Exception:  # noqa: BLE001 - monitoring must not crash
                    log.warning("feed check_stale failed", exc_info=True)
        # Recovery runs before the watchdog/cadence checks so a DEGRADED feed is
        # handed to the fail-closed coordinator on the first poll that can act on
        # it. The coordinator writes RESYNCHRONIZING then READY/HALTED itself;
        # this method never sets the state, so the fail-closed decision stays in
        # one place. A coordinator that raises must not take the poll down.
        if was_degraded and self._feed_recovery is not None:
            try:
                self._feed_recovery.recover()
            except Exception:  # noqa: BLE001 - monitoring must not crash
                log.warning("feed recovery failed", exc_info=True)
        watchdog = self._order_watchdog
        if watchdog is not None:
            try:
                watchdog.check()
            except Exception:  # noqa: BLE001 - monitoring must not crash
                log.warning("order stream watchdog check failed", exc_info=True)
        cadence = self._cadence_monitor
        if cadence is not None:
            try:
                cadence.check()
            except Exception:  # noqa: BLE001 - monitoring must not crash
                log.warning("per-symbol cadence check failed", exc_info=True)
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


#: A subscribed symbol must deliver at least one tick in any window this wide
#: during session hours (AC-12, lane 3d).
#:
#: Three minutes, not the feed's 30 s, because the question is per symbol. A 30 s
#: threshold on a per-symbol clock would alarm on every thinly traded scrip, and
#: an alarm that cries wolf on the illiquid is no alarm at all. Three minutes is
#: the window the programme names, and it is short enough that a genuinely dead
#: symbol is caught inside a single NIFTY bar.
CADENCE_WINDOW_SECONDS = CADENCE_WINDOW

#: How long a symbol must have been on the *wanted* set before a window with no
#: tick at all in it counts against it.
#:
#: A subscription alone does not prove liveness, so the clock is not started at
#: ``subscribe()``: the monitor cannot see when a symbol was subscribed, and a
#: cold boot subscribes the whole tape at once, where the first seconds
#: legitimately deliver nothing for the back of the book. It is started at the
#: monitor's *first sighting* of the symbol instead, so a scrip that joins the
#: tape mid-session gets the same full window a boot-time symbol does.
#:
#: This is what keeps the check honest about the case that matters most. A
#: symbol that has produced **no** ticks at all is not a young subscription
#: forever — it is exactly the failure the lane exists for, and it is the one
#: :meth:`MarketFeed.check_stale` structurally cannot see (the symbol is absent
#: from ``_last_tick`` rather than stale in it). Treating "never ticked" as
#: permanently unjudgeable would leave the headline failure unmonitored behind a
#: graceful-sounding rule.
CADENCE_GRACE_SECONDS = 180.0

#: Instrument-master lifecycle states that exempt a symbol from the cadence
#: contract. The vocabulary itself — which vendor spellings map to which state —
#: lives in ``tradex_domain.instrument_lifecycle``, because the two master
#: parsers read their vendor's status column through that same table and this
#: module consumes what they wrote. A private copy here would be a third
#: spelling, and a drift is silent in the worst direction: the exemption stops
#: applying and every halted stock on the tape pages the operator forever.
#:
#: The rule that matters: an *unrecognised* status is not an exemption. An
#: unknown word is a reason to keep looking, not a reason to stop.
EXEMPT_MASTER_STATES = EXEMPT_STATES


def normalize_master_status(value: object) -> str | None:
    """Canonical lifecycle state for a master status cell, or ``None``.

    A thin, named alias of the domain vocabulary so a reader of this module
    (and its tests) can talk about "master status" without knowing which layer
    defines it. ``None`` means the master said nothing usable, and therefore
    *not* exempt: a blank cell, ``"active"`` and ``"something new"`` all land
    here, so the symbol stays under the cadence contract.
    """
    return normalize_status(value)


def master_row_status(row: object) -> str | None:
    """The canonical lifecycle state a master *row* carries, or ``None``.

    Reads a normalized row's ``master_status`` or any vendor's own status
    column, so a caller can hand it a raw CSV/JSON mapping or an
    already-parsed row without knowing which.
    """
    return row_status(row)


def master_exempt(row: object) -> bool:
    """Whether a master row's lifecycle state exempts its symbol from cadence."""
    return row_status(row) in EXEMPT_MASTER_STATES


def _as_ist(moment: datetime) -> datetime:
    """*moment* as an aware IST datetime, whatever awareness it arrived with.

    The feed stamps last-tick times in UTC (``datetime.now(UTC)``) and the
    datalake contract stores naive IST; a check that subtracted one from the
    other would raise rather than answer. Normalizing at the boundary is what
    lets a live feed and a replayed tape share one cadence implementation.
    """
    if moment.tzinfo is None:
        return moment.replace(tzinfo=IST)
    return moment.astimezone(IST)


def in_session_hours(
    now: datetime, *, window: tuple[dt_time, dt_time] | None = None
) -> bool:
    """Whether *now* falls inside the cash session, in IST wall time.

    The bounds are the market calendar's :data:`MARKET_OPEN` / :data:`MARKET_CLOSE`
    — the same two values ``tradex_domain.timeframe`` anchors the bar grid to —
    read here rather than restated, so the cadence check and the bars it judges
    cannot disagree about when the day begins or ends. Lane 0d owns that anchor;
    a second copy of "09:15" in this module would be a second session.

    Both ends are inclusive, matching ``FeedIntegrityTracker._session_violation``
    (``start <= time <= end``): a stamp at exactly 15:30 is the closing print
    and still belongs to the session.

    Naive input is read as already-IST, the datalake contract; aware input is
    converted to IST first, so a UTC clock cannot silently check the wrong day.
    """
    local = now.astimezone(IST) if now.tzinfo is not None else now.replace(tzinfo=IST)
    start, end = window or (MARKET_OPEN, MARKET_CLOSE)
    return start <= local.time() <= end


@dataclass(frozen=True, slots=True)
class CadenceViolation:
    """One subscribed symbol that produced no tick inside a cadence window.

    ``quiet_seconds`` is the age of the silence, not the age of the last tick:
    the window is measured from the far edge of the bucket the last tick fell
    in, which is the instant the gap became provable.
    """

    instrument: Any
    instrument_id: str
    quiet_seconds: float


class SymbolCadenceMonitor:
    """Per-symbol tick cadence: one tick per window, in session hours (AC-12).

    Why this is not :meth:`MarketFeed.check_stale`
    ------------------------------------------------
    ``check_stale`` is a *tape* check: a 30 s threshold over instruments that
    have already ticked, since ``_last_tick`` is only written by ``_on_quote``.
    It answers "is the socket producing at all", which is exactly what a 30 s
    threshold should answer, and it is right to. It cannot answer "is *this*
    symbol alive", because a symbol the wire stops delivering for is *absent*
    from that state rather than stale in it — a feed can be perfectly healthy by
    every measure ``check_stale`` makes while one scrip has gone dark.

    Neither wire format carries a sequence number, so a dropped frame is
    indistinguishable from an illiquid minute, and only a cadence expectation
    separates them. Merging the two checks would force a choice that is wrong
    either way: fold cadence into ``check_stale`` and a genuinely dead socket
    takes three minutes to notice, or keep 30 s and every thinly traded scrip
    alarms. So they stay separate, answer different questions, and never
    disagree: the cadence monitor's verdict is per symbol and goes to its own
    counters. It does not demote the feed, because one illiquid or delisted
    scrip must never shut the order gate for a healthy tape — the fail direction
    there is "keep the symbol visible", not "refuse to trade".

    Why it does not own a thread
    -----------------------------
    It is polled by :meth:`FeedStaleMonitor.poll_once`, the same owned daemon
    thread that already polls the quote tape and the order stream — the same
    one-lifecycle-per-timer rule :class:`OrderStreamWatchdog` follows. A second
    thread would have to be started, stopped, and made exception-safe a second
    time for a check that runs in microseconds.

    The exemption, and why it is counted
    -------------------------------------
    A halted or delisted security produces no ticks *by design*. Held to the same
    contract it would alarm on every poll for the rest of the session, every
    session, forever — and an alert that fires every day is an alert nobody
    reads, so the check would be worthless in exactly the way this lane exists
    to prevent. So the instrument master's lifecycle state exempts the symbol.

    But a silent exemption is the opposite failure: a dead symbol quietly
    reclassified as "halted" and never looked at again, which is a blind spot
    wearing an exemption. So every exemption increments
    ``feed_liveness_exempt_total`` (labelled by the canonical state) *once per
    symbol per state*, and ``feed_liveness_exempt_symbols`` carries the size of
    the blind spot as it stands right now. The counter is the audit trail, and
    it is armed in ``deploy/prometheus-rules.yml``: a rising exemption count is
    coverage quietly being withdrawn, and a sudden jump is far more likely to be
    a truncated or mis-parsed master than a genuinely halted universe.
    """

    def __init__(
        self,
        *,
        instruments: Callable[[], Iterable[Any]] | None = None,
        ticks: Callable[[Any], datetime | None] | None = None,
        status_of: Callable[[Any], object] | None = None,
        metrics: Any | None = None,
        window_seconds: float = CADENCE_WINDOW_SECONDS,
        grace_seconds: float = CADENCE_GRACE_SECONDS,
        clock: Callable[[], datetime] | None = None,
        session_window: tuple[dt_time, dt_time] | None = None,
    ) -> None:
        self._instruments = instruments
        self._ticks = ticks
        self._status_of = status_of
        self._metrics = metrics
        self._window = float(window_seconds)
        self._grace = float(grace_seconds)
        self._clock = clock or (lambda: datetime.now(UTC))
        self._session_window = session_window
        self._lock = threading.Lock()
        #: Currently-quiet symbols, so a continuing silence is one log line and
        #: one metric reading rather than one per poll, and a symbol that
        #: resumes clears its verdict on the next check.
        self._reported: dict[str, CadenceViolation] = {}
        #: (instrument_id, state) pairs already counted, so a symbol exempt
        #: every poll for six hours counts once per state rather than 40,000
        #: times — while still being *counted at all*, which is the requirement.
        self._exempt_counted: set[tuple[str, str]] = set()
        #: The blind spot as it stands right now, which is a different and more
        #: urgent fact than the lifetime total.
        self._exempt_now: set[str] = set()
        #: First sweep at which each symbol was seen on the wanted set. This is
        #: the start of the no-tick-at-all window (see
        #: :data:`CADENCE_GRACE_SECONDS`): without it a symbol that never
        #: delivers a single frame could never be judged, which is precisely
        #: the failure this check exists to catch. Dropped for symbols that
        #: leave the tape, so a resubscribe starts a fresh window.
        self._seen_since: dict[str, datetime] = {}
        self._off_session_polls = 0

    # -- introspection --------------------------------------------------------

    @property
    def window_seconds(self) -> float:
        """The cadence window, in seconds."""
        return self._window

    @property
    def grace_seconds(self) -> float:
        """Silence tolerated from the first tick before the contract starts."""
        return self._grace

    @property
    def off_session_polls(self) -> int:
        """Polls skipped because the market was closed."""
        return self._off_session_polls

    @property
    def exempt_now(self) -> frozenset[str]:
        """Instrument ids currently exempt from the cadence contract."""
        with self._lock:
            return frozenset(self._exempt_now)

    @property
    def violations(self) -> tuple[CadenceViolation, ...]:
        """Symbols currently reported as having gone quiet."""
        with self._lock:
            return tuple(self._reported.values())

    # -- input ----------------------------------------------------------------

    def reset(self) -> None:
        """Forget the quiet set, the sighting clock and the exemption memory."""
        with self._lock:
            self._reported.clear()
            self._exempt_counted.clear()
            self._exempt_now.clear()
            self._seen_since.clear()

    def check(self) -> list[CadenceViolation]:
        """Evaluate every subscribed symbol once; return the current violations.

        Never raises. This runs inside the same poll as the tape and order-stream
        checks, so a raise here would take all three down; a monitor that dies is
        a monitor that stopped monitoring. A per-symbol error degrades to "not
        exempt, not judged", which is the fail-loud direction.

        Off session there is nothing to judge, so the whole sweep is skipped: a
        quiet tape at 20:00 is not a defect, and a check that ran anyway would
        alarm on every symbol for seven hours a day.
        """
        now = self._clock()
        if not in_session_hours(now, window=self._session_window):
            with self._lock:
                self._off_session_polls += 1
                # A verdict does not outlive the session that produced it: the
                # first ticks of a new day are "late" relative to the quiet
                # window the last one ended in, by construction.
                self._reported.clear()
            return []
        try:
            instruments = list(self._instruments()) if self._instruments else []
        except Exception:  # noqa: BLE001 - never take the poll down
            log.warning("per-symbol cadence could not read the tape", exc_info=True)
            return []
        violations: list[CadenceViolation] = []
        exempt_now: set[str] = set()
        present: set[str] = set()
        for instrument in instruments:
            try:
                iid = str(getattr(instrument, "instrument_id", instrument))
                present.add(iid)
                state = self._exempt_state(instrument)
                if state is not None:
                    exempt_now.add(iid)
                    self._count_exempt(iid, state)
                    continue
                quiet_since = self._quiet_since(instrument, now)
                if quiet_since is None:
                    continue
                # Both sides aware-IST: ``_quiet_since`` normalizes, and the
                # caller's clock may be UTC (``datetime.now(UTC)``) or naive
                # IST (a replay). ``_as_ist`` is what keeps the two comparable.
                quiet = (_as_ist(now) - _as_ist(quiet_since)).total_seconds()
                if quiet < self._window:
                    continue
                violations.append(
                    CadenceViolation(
                        instrument=instrument,
                        instrument_id=iid,
                        quiet_seconds=quiet,
                    )
                )
            except Exception:  # noqa: BLE001 - one bad symbol, not a dead check
                log.warning("per-symbol cadence could not judge a symbol", exc_info=True)
        with self._lock:
            # A symbol that left the tape is forgotten, so a later resubscribe
            # earns a fresh window rather than inheriting an old verdict.
            for gone in set(self._seen_since) - present:
                del self._seen_since[gone]
        self._publish(violations, exempt_now)
        return violations

    # -- internals ------------------------------------------------------------

    def _exempt_state(self, instrument: Any) -> str | None:
        """Canonical exempt state for *instrument*, or ``None``.

        Two sources, and both end up in the same counter:

        1. ``status_of`` — an injected resolver. Boot passes one bound to the
           broker's instrument registry, so a master row's lifecycle state is
           read where the master actually landed (the registry's per-instrument
           meta is the master row's metadata by construction).
        2. ``Instrument.meta.extra`` — the value object's own metadata, so an
           instrument built straight from a master row is exempt without a
           registry lookup. This is what makes the exemption testable and usable
           off a full live boot.
        """
        if self._status_of is not None:
            state = normalize_master_status(self._status_of(instrument))
            if state in EXEMPT_MASTER_STATES:
                return state
        meta = getattr(instrument, "meta", None)
        extra = getattr(meta, "extra", None) if meta is not None else None
        if isinstance(extra, dict):
            return normalize_master_status(extra.get("master_status"))
        return None

    def _count_exempt(self, iid: str, state: str) -> None:
        """Count this symbol's exemption once per state and log it once."""
        with self._lock:
            key = (iid, state)
            if key in self._exempt_counted:
                return
            self._exempt_counted.add(key)
        metrics = self._metrics
        if metrics is not None:
            # Labels ride the ``inc()`` call, not the family lookup: a family
            # created without them would publish an unlabelled series and every
            # reader would be summing blind spots across states.
            metrics.counter("feed_liveness_exempt_total").inc(1, state=state)
        log.info(
            "per-symbol cadence exempting %s (instrument master reports %s): a "
            "security in this state produces no ticks by design — counted in "
            "feed_liveness_exempt_total so the blind spot stays visible",
            iid, state,
        )

    def _quiet_since(self, instrument: Any, now: datetime) -> datetime | None:
        """Start of *instrument*'s current empty window, or ``None`` if healthy.

        Two cases, and the distinction between them is the whole point of the
        class:

        **The symbol has ticked.** The window is measured on the
        *session-anchored* grid from ``tradex_domain.timeframe.bucket_start``
        (lane 0d), not on a rolling wall clock from the last tick. A tick at
        09:16 falls in the 09:15-anchored 09:15-09:18 bucket, so its window
        closes at 09:18 — the same grid the bars themselves are cut on. A second
        window arithmetic here would be a second session definition, and the
        cadence would disagree with the bars by up to one window width.

        **The symbol has never ticked.** The window runs from the monitor's
        first sighting of it on the wanted set (:data:`CADENCE_GRACE_SECONDS`).
        This is the case ``check_stale`` cannot see at all — a symbol the wire
        never delivers for is absent from ``_last_tick``, not stale in it — so
        it is the one that most needs a verdict. A brand-new subscription is
        not judged until its window has elapsed, which is what keeps a cold
        boot from alarming on the whole tape at once.
        """
        now_ist = _as_ist(now)
        iid = str(getattr(instrument, "instrument_id", instrument))
        last = self._ticks(instrument) if self._ticks is not None else None
        if isinstance(last, datetime):
            # ``MarketFeed._last_tick`` stamps aware UTC while a replayed tape
            # carries the datalake's naive IST, and ``bucket_start`` is naive
            # IST either way; ``_as_ist`` is what lets the two coexist rather
            # than raise a ``TypeError`` the per-symbol guard would swallow.
            last_ist = _as_ist(last)
            bucket = bucket_start(last_ist.replace(tzinfo=None), int(self._window))
            return (bucket + timedelta(seconds=self._window)).replace(tzinfo=IST)
        if self._grace <= 0:
            return self._seen_since.get(iid, now_ist)
        with self._lock:
            first = self._seen_since.setdefault(iid, now_ist)
        return first

    def _publish(self, violations: list[CadenceViolation], exempt_now: set[str]) -> None:
        metrics = self._metrics
        if metrics is not None:
            metrics.gauge("feed_liveness_silent_symbols").set(float(len(violations)))
            metrics.gauge("feed_liveness_exempt_symbols").set(float(len(exempt_now)))
        current = {v.instrument_id: v for v in violations}
        with self._lock:
            for iid, violation in current.items():
                if iid not in self._reported:
                    log.warning(
                        "no tick for %s in any %.0fs window during session hours "
                        "(quiet %.0fs): a dropped frame is indistinguishable "
                        "from an illiquid minute and only the cadence tells "
                        "them apart",
                        iid, self._window, violation.quiet_seconds,
                    )
            self._reported = current
            self._exempt_now = set(exempt_now)


__all__ = [
    "CADENCE_GRACE_SECONDS",
    "CADENCE_WINDOW_SECONDS",
    "EXEMPT_MASTER_STATES",
    "FeedStaleMonitor",
    "ORDER_FEED_STALE_AFTER",
    "CadenceViolation",
    "OrderStreamWatchdog",
    "SymbolCadenceMonitor",
    "in_session_hours",
    "master_exempt",
    "master_row_status",
    "normalize_master_status",
]
