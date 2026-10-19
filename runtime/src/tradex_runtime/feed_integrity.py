"""Feed integrity accounting — gaps, duplicates, ordering, session bounds.

A live broker tape is not guaranteed to be complete, ordered, or duplicate
free (reconnects replay, exchanges re-send, the backend can drop frames under
load). This module is the single place that *counts* those defects so they are
observable (metrics + ``FeedGapDetected``) instead of silently shaping bars.

Design
------
* Pure accounting: the tracker never publishes and never blocks the tape.
  Callers (``MarketFeed``) feed it quotes and closed bar frames, publish the
  returned ``GapKind`` values, and export the snapshot as metrics.
* Bounded memory: the duplicate window is a fixed-size FIFO, so a long
  session cannot grow the process by remembering every tick ever seen.
* Fail-open: a violation is recorded and the tick still flows. Feed *health*
  (READY/DEGRADED/HALTED) is owned by :class:`FeedSupervisor`, not here.
"""

from __future__ import annotations

import threading
from collections import deque
from dataclasses import dataclass
from datetime import datetime, time
from enum import StrEnum
from typing import Any

from tradex_domain.constants import DEDUP_WINDOW
from tradex_domain.enums import Timeframe
from tradex_domain.market_calendar import IST, MARKET_CLOSE, MARKET_OPEN
from tradex_domain.timeframe import bucket_seconds

_IST = IST

#: Exchanges whose cash session is 09:15-15:30 IST.
_CASH_EXCHANGES = frozenset({"NSE", "BSE", "IDX"})
#: Number of event keys retained for duplicate detection.
_DEFAULT_DEDUP_WINDOW = DEDUP_WINDOW
#: Default cash session window (09:15-15:30 IST) for session-boundary checks.
_DEFAULT_SESSION_WINDOW: tuple[time, time] = (MARKET_OPEN, MARKET_CLOSE)


def _bucket_seconds_or_none(timeframe: str) -> int | None:
    """Bucket width in seconds for a canonical timeframe string, else ``None``."""
    try:
        return bucket_seconds(Timeframe(timeframe))
    except (KeyError, ValueError):
        return None


class GapKind(StrEnum):
    """Category of a feed integrity violation."""

    DUPLICATE_EVENT = "duplicate_event"
    OUT_OF_ORDER = "out_of_order"
    LARGE_JUMP = "large_jump"
    SESSION_BOUNDARY = "session_boundary"
    DUPLICATE_CLOSED_BAR = "duplicate_closed_bar"
    MISSING_BAR = "missing_bar"


#: Metric increments applied per violation kind.
_KIND_METRICS: dict[GapKind, tuple[str, ...]] = {
    GapKind.DUPLICATE_EVENT: ("feed_gaps_total", "feed_duplicate_events_total"),
    GapKind.OUT_OF_ORDER: ("feed_gaps_total", "feed_out_of_order_events_total"),
    GapKind.LARGE_JUMP: ("feed_gaps_total", "feed_large_jumps_total"),
    GapKind.SESSION_BOUNDARY: (
        "feed_gaps_total",
        "feed_session_boundary_violations_total",
    ),
    GapKind.DUPLICATE_CLOSED_BAR: (
        "feed_gaps_total",
        "feed_duplicate_closed_bars_total",
    ),
    GapKind.MISSING_BAR: ("feed_gaps_total", "feed_missing_bars_total"),
}


@dataclass(frozen=True, slots=True)
class FeedIntegritySnapshot:
    """Point-in-time view of every counter held by a tracker."""

    events_seen: int = 0
    bars_seen: int = 0
    duplicate_events: int = 0
    out_of_order_events: int = 0
    large_jumps: int = 0
    session_boundary_violations: int = 0
    duplicate_closed_bars: int = 0
    missing_bars: int = 0

    @property
    def total_violations(self) -> int:
        """Sum of every violation counter."""
        return (
            self.duplicate_events
            + self.out_of_order_events
            + self.large_jumps
            + self.session_boundary_violations
            + self.duplicate_closed_bars
            + self.missing_bars
        )


class FeedIntegrityTracker:
    """Thread-safe gap/duplicate accounting for one market feed.

    ``on_quote``/``on_depth``/``on_bar`` return the violations observed for
    that single input so a caller can publish them; counters accumulate
    independently and are exposed via :meth:`snapshot`.
    """

    def __init__(
        self,
        *,
        large_jump_seconds: float = 60.0,
        metrics: Any | None = None,
        dedup_window: int = _DEFAULT_DEDUP_WINDOW,
        session_window: tuple[time, time] = _DEFAULT_SESSION_WINDOW,
    ) -> None:
        self._large_jump_seconds = float(large_jump_seconds)
        self._metrics = metrics
        self._session_window = session_window
        self._dedup_window = max(1, int(dedup_window))
        self._seen: deque[Any] = deque()
        self._seen_set: set[Any] = set()
        self._last_quote_ts: dict[Any, datetime] = {}
        self._last_bar_time: dict[tuple[str, str], int] = {}
        self._session_flagged: set[tuple[Any, Any]] = set()
        self._lock = threading.Lock()
        self._events_seen = 0
        self._bars_seen = 0
        self._duplicate_events = 0
        self._out_of_order_events = 0
        self._large_jumps = 0
        self._session_boundary_violations = 0
        self._duplicate_closed_bars = 0
        self._missing_bars = 0

    # -- public API ---------------------------------------------------------

    def on_quote(self, quote: Any) -> tuple[GapKind, ...]:
        """Account one quote; return the violations it represents."""
        iid = quote.instrument.instrument_id
        ts = getattr(quote, "timestamp", None)
        key = self._quote_key(quote)
        violations: list[GapKind] = []
        with self._lock:
            self._events_seen += 1
            if key is not None:
                if key in self._seen_set:
                    violations.append(GapKind.DUPLICATE_EVENT)
                else:
                    self._remember(key)
            previous = self._last_quote_ts.get(iid)
            if isinstance(ts, datetime) and isinstance(previous, datetime):
                if ts < previous:
                    violations.append(GapKind.OUT_OF_ORDER)
                elif (ts - previous).total_seconds() > self._large_jump_seconds:
                    violations.append(GapKind.LARGE_JUMP)
            if isinstance(ts, datetime):
                if previous is None or ts > previous:
                    self._last_quote_ts[iid] = ts
                violation = self._session_violation(iid, quote, ts)
                if violation is not None:
                    violations.append(violation)
            self._count(violations)
        return tuple(violations)

    def on_depth(self, depth: Any) -> tuple[GapKind, ...]:
        """Account one depth frame (duplicates only — no timestamp ordering)."""
        key = (
            "depth",
            str(depth.instrument.instrument_id),
            len(depth.bids),
            len(depth.asks),
        )
        with self._lock:
            self._events_seen += 1
            violations: list[GapKind] = []
            if key in self._seen_set:
                violations.append(GapKind.DUPLICATE_EVENT)
            else:
                self._remember(key)
            self._count(violations)
        return tuple(violations)

    def on_bar(self, frame: Any) -> tuple[GapKind, ...]:
        """Account one closed bar frame; return duplicate/missing violations.

        Forming frames are ignored: only a closed bar participates in the
        (instrument, timeframe) sequence contract.
        """
        if not bool(getattr(frame, "closed", False)):
            return ()
        key = (str(frame.instrument), str(frame.timeframe))
        bar_time = int(frame.time)
        violations: list[GapKind] = []
        with self._lock:
            self._bars_seen += 1
            previous = self._last_bar_time.get(key)
            if previous is None:
                self._last_bar_time[key] = bar_time
                return ()
            if bar_time == previous:
                self._duplicate_closed_bars += 1
                violations.append(GapKind.DUPLICATE_CLOSED_BAR)
            elif bar_time > previous:
                step = _bucket_seconds_or_none(key[1])
                if step:
                    missing = (bar_time - previous) // step - 1
                    if missing > 0:
                        self._missing_bars += int(missing)
                        violations.append(GapKind.MISSING_BAR)
                self._last_bar_time[key] = bar_time
            else:  # an older closed bar replayed after a newer one
                self._out_of_order_events += 1
                violations.append(GapKind.OUT_OF_ORDER)
            for kind in violations:
                self._inc_metrics(kind)
        return tuple(violations)

    def snapshot(self) -> FeedIntegritySnapshot:
        """Return every counter as an immutable snapshot."""
        with self._lock:
            return FeedIntegritySnapshot(
                events_seen=self._events_seen,
                bars_seen=self._bars_seen,
                duplicate_events=self._duplicate_events,
                out_of_order_events=self._out_of_order_events,
                large_jumps=self._large_jumps,
                session_boundary_violations=self._session_boundary_violations,
                duplicate_closed_bars=self._duplicate_closed_bars,
                missing_bars=self._missing_bars,
            )

    def reset(self) -> None:
        """Drop all counters and per-instrument state (generation reset)."""
        with self._lock:
            self._seen.clear()
            self._seen_set.clear()
            self._last_quote_ts.clear()
            self._last_bar_time.clear()
            self._session_flagged.clear()
            self._events_seen = 0
            self._bars_seen = 0
            self._duplicate_events = 0
            self._out_of_order_events = 0
            self._large_jumps = 0
            self._session_boundary_violations = 0
            self._duplicate_closed_bars = 0
            self._missing_bars = 0

    # -- internals ----------------------------------------------------------

    def _quote_key(self, quote: Any) -> Any | None:
        """Stable identity for duplicate detection.

        Prefers an explicit provider event id (``metadata['event_id']`` /
        ``metadata['sequence']``); otherwise a content key of
        (instrument, timestamp, price, volume) within the dedup window.
        """
        metadata = getattr(quote, "metadata", None) or {}
        for name in ("event_id", "sequence", "seq"):
            if name in metadata:
                return ("id", str(quote.instrument.instrument_id), str(metadata[name]))
        ts = getattr(quote, "timestamp", None)
        if not isinstance(ts, datetime):
            return None
        volume = getattr(quote, "volume", None)
        volume_value = getattr(volume, "value", volume)
        ltp = getattr(quote, "ltp", None)
        price = getattr(ltp, "value", ltp)
        return (
            "content",
            str(quote.instrument.instrument_id),
            ts.isoformat(),
            str(price),
            str(volume_value),
        )

    def _remember(self, key: Any) -> None:
        self._seen.append(key)
        self._seen_set.add(key)
        while len(self._seen) > self._dedup_window:
            self._seen_set.discard(self._seen.popleft())

    def _session_violation(
        self, iid: Any, quote: Any, ts: datetime
    ) -> GapKind | None:
        """Flag a cash-session tick stamped outside 09:15-15:30 IST, once/day."""
        exchange = getattr(getattr(quote, "instrument", None), "exchange", None)
        name = str(getattr(exchange, "value", exchange) or "")
        if name not in _CASH_EXCHANGES or _IST is None:
            return None
        local = ts.astimezone(_IST) if ts.tzinfo is not None else ts.replace(tzinfo=_IST)
        start, end = self._session_window
        if start <= local.time() <= end:
            return None
        flag = (iid, local.date())
        if flag in self._session_flagged:
            return None
        self._session_flagged.add(flag)
        self._session_boundary_violations += 1
        return GapKind.SESSION_BOUNDARY

    def _count(self, violations: list[GapKind]) -> None:
        for kind in violations:
            if kind is GapKind.DUPLICATE_EVENT:
                self._duplicate_events += 1
            elif kind is GapKind.OUT_OF_ORDER:
                self._out_of_order_events += 1
            elif kind is GapKind.LARGE_JUMP:
                self._large_jumps += 1
            # SESSION_BOUNDARY counter is bumped in _session_violation.
            self._inc_metrics(kind)

    def _inc_metrics(self, kind: GapKind) -> None:
        if self._metrics is None:
            return
        for name in _KIND_METRICS.get(kind, ()):  # tiny closed table
            self._metrics.counter(name).inc()


__all__ = [
    "FeedIntegritySnapshot",
    "FeedIntegrityTracker",
    "GapKind",
]

