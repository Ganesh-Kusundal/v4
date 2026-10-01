"""Feed recovery orchestration after a broker socket reconnect.

A reconnect only proves the socket is up. It does not prove the local view of
the tape is continuous: ticks and closed bars can be missing for the whole
disconnect window. This module owns the fail-closed sequence that turns a live
stream back into READY:

1. mark the feed ``RESYNCHRONIZING`` (never READY) and remember the last
   accepted event timestamp;
2. fetch history for every subscribed instrument from the last closed bar
   (or the last accepted event) through now;
3. de-duplicate the recovered closed bars and count any missing bars;
4. hand the reconciled bars to the seeding hook;
5. publish ``FeedRecovered`` and only then transition to READY.

If history cannot be fetched or the replay proves a hole larger than the
configured allowance, recovery fails and the feed goes ``HALTED`` — the
session keeps rejecting live orders instead of trading a blind book.
"""

from __future__ import annotations

import logging
import threading
import time
from collections.abc import Callable
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from typing import Any, Protocol, runtime_checkable

from tradex_domain.enums import Timeframe
from tradex_domain.events import FeedRecovered
from tradex_domain.timeframe import bucket_seconds, interval_seconds

from tradex_runtime.feed_supervisor import FeedSupervisor

log = logging.getLogger(__name__)

#: Default cold-start recovery window when neither a last closed bar nor a last
#: accepted event is known: one day of bars, from the canonical registry.
_LOOKBACK_SECONDS = float(bucket_seconds(Timeframe.D1))

#: Default tolerance, in bars, for a hole between the last accepted bar and the
#: first replayed one. Non-zero so the fail-closed contract holds without
#: configuration, and set above the largest hole a healthy venue produces: Dhan's
#: NSE 1m series stops at 15:14, so the last 15 minutes of every session are
#: structurally unavailable (CLAUDE.md, "Venue ceilings"). Below 15 the feed
#: would HALT on the known tail and never recover.
#:
#: ponytail: one flat allowance for the whole session, so it cannot tell a
#: structural session-edge hole from an interior one. That distinction is the
#: upgrade path -- ``GapDetector.scan()`` already classifies edge-only
#: shortfalls, and the ceiling here is that a 20-bar interior hole and a
#: 20-bar tail hole are treated alike.
_DEFAULT_MAX_MISSING_BARS = 20


@runtime_checkable
class HistoryProvider(Protocol):
    """Source of closed bars used to re-seed a reconnecting feed."""

    def fetch_bars(
        self,
        instrument: Any,
        timeframe: str,
        start: datetime,
        end: datetime,
    ) -> Any:
        """Return an iterable of bars (``Candle`` or ``BarFrame``) for the window.

        ``start`` is required: the only production caller derives it from
        ``_start_for``, which always returns a real ``datetime``, and every
        concrete provider forwards it to an API that needs a bound.
        """
        ...


@dataclass(frozen=True, slots=True)
class RecoveryOutcome:
    """Result of one recovery attempt."""

    ok: bool
    generation: int = 0
    instruments: tuple[str, ...] = ()
    bars_replayed: int = 0
    missing_bars: int = 0
    recovered_through: datetime | None = None
    error: str | None = None
    duration_seconds: float = 0.0



def _bar_epoch(bar: Any) -> int | None:
    """Extract a UTC epoch-seconds bucket start from a Candle or BarFrame."""
    time_value = getattr(bar, "time", None)
    if isinstance(time_value, (int, float)):
        return int(time_value)
    ts = getattr(bar, "timestamp", None)
    if isinstance(ts, datetime):
        if ts.tzinfo is None:
            return int(ts.replace(tzinfo=UTC).timestamp())
        return int(ts.timestamp())
    return None


def _bar_instrument(bar: Any) -> str:
    instrument = getattr(bar, "instrument", None)
    if instrument is None:
        return ""
    iid = getattr(instrument, "instrument_id", instrument)
    return str(iid)


def _bar_timeframe(bar: Any, default: str) -> str:
    tf = getattr(bar, "timeframe", None)
    if tf is None:
        return default
    return str(getattr(tf, "value", tf))


class FeedRecoveryCoordinator:
    """Runs the fail-closed reconnect recovery sequence for one feed.

    Parameters
    ----------
    feed:
        ``MarketFeed`` — used for its supervisor, subscribed instruments and
        (optionally) re-subscription.
    history_provider:
        Bars source. When ``None`` every recovery attempt fails closed: a
        reconnect that cannot replay history cannot prove continuity.
    on_history:
        Seeding hook called with the de-duplicated, sorted recovered bars so
        the bar aggregator can be primed instead of starting from a hole.
    bus:
        Optional reactive bus; ``FeedRecovered`` is published on success.
    timeframe:
        Default timeframe when a bar carries none.
    lookback_seconds:
        Window used when neither a last closed bar nor a last accepted event
        is known (cold start after a long outage).
    max_missing_bars:
        How many bars of hole recovery tolerates between the last accepted bar
        and the replayed series before it fails closed. ``0`` means every hole
        halts the feed; the default absorbs only holes smaller than a venue's
        structural one (see :data:`_DEFAULT_MAX_MISSING_BARS`). Must be >= 0.
    """

    def __init__(
        self,
        feed: Any,
        *,
        supervisor: FeedSupervisor | None = None,
        history_provider: HistoryProvider | None = None,
        on_history: Callable[[tuple[Any, ...]], None] | None = None,
        bus: Any | None = None,
        timeframe: str = Timeframe.M1.value,
        lookback_seconds: float = _LOOKBACK_SECONDS,
        max_missing_bars: int = _DEFAULT_MAX_MISSING_BARS,
        metrics: Any | None = None,
        clock: Callable[[], datetime] | None = None,
    ) -> None:
        if max_missing_bars < 0:
            raise ValueError(
                f"max_missing_bars must be >= 0, got {max_missing_bars!r}"
            )
        self._feed = feed
        self._supervisor = supervisor
        self._history_provider = history_provider
        self._on_history = on_history
        self._bus = bus
        self._timeframe = str(timeframe)
        self._lookback_seconds = float(lookback_seconds)
        self._max_missing_bars = int(max_missing_bars)
        self._metrics = metrics
        self._clock = clock or (lambda: datetime.now(UTC))
        self._closed_bars: dict[str, Any] = {}
        self._lock = threading.RLock()

    # -- state fed by the live path -----------------------------------------

    def record_closed_bar(self, frame: Any) -> None:
        """Remember the newest closed bar so recovery knows where to resume."""
        iid = str(getattr(frame, "instrument", "") or "")
        epoch = _bar_epoch(frame)
        if not iid or epoch is None:
            return
        with self._lock:
            previous = self._closed_bars.get(iid)
            if previous is None or epoch >= (_bar_epoch(previous) or 0):
                self._closed_bars[iid] = frame

    def last_closed_bar(self, instrument_id: Any) -> Any | None:
        """The newest closed bar recorded for *instrument_id*, if any."""
        with self._lock:
            return self._closed_bars.get(str(instrument_id))

    # -- recovery -----------------------------------------------------------

    def recover(self) -> RecoveryOutcome:
        """Run the fail-closed recovery sequence; return the outcome."""
        supervisor: FeedSupervisor | None = self._supervisor or getattr(
            self._feed, "supervisor", None
        )
        started = time.monotonic()
        if supervisor is not None:
            supervisor.recovery_started()
        instruments = self._subscribed_instruments()
        names = tuple(str(getattr(i, "instrument_id", i)) for i in instruments)
        if not instruments:
            return self._fail(supervisor, "no instruments subscribed", started)
        if self._history_provider is None:
            return self._fail(
                supervisor,
                "no history provider: a reconnect alone cannot prove tape continuity",
                started,
            )
        last_event_at = getattr(supervisor, "last_event_at", None)
        end = self._clock()
        recovered: list[Any] = []
        errors: list[str] = []
        missing = 0
        for inst in instruments:
            start = self._start_for(inst, last_event_at, end)
            try:
                bars = self._history_provider.fetch_bars(inst, self._timeframe, start, end)
            except Exception as exc:  # noqa: BLE001 - collected, then fail closed
                errors.append(f"{inst.instrument_id}: {exc}")
                continue
            deduped = self._dedupe(bars)
            recovered.extend(deduped)
            missing += self._missing_between(inst, deduped, last_event_at)
        if errors:
            return self._fail(supervisor, "; ".join(errors), started)
        if not recovered and last_event_at is None:
            return self._fail(supervisor, "history replay returned no bars", started)
        # F-43: the count is the gate, not a metric. A hole this wide means the
        # local view of the tape is discontinuous, and seeding the aggregator
        # from a discontinuous series would trade bars that never happened.
        if missing > self._max_missing_bars:
            return self._fail(
                supervisor,
                f"history replay proved {missing} missing bars, over the "
                f"allowance of {self._max_missing_bars}",
                started,
            )
        recovered.sort(key=lambda bar: _bar_epoch(bar) or 0)
        recovered_through = last_event_at
        for bar in recovered:
            epoch = _bar_epoch(bar)
            if epoch is None:
                continue
            ts = datetime.fromtimestamp(epoch, tz=UTC)
            if recovered_through is None or ts > recovered_through:
                recovered_through = ts
        if self._on_history is not None and recovered:
            try:
                self._on_history(tuple(recovered))
            except Exception as exc:  # noqa: BLE001 - seeding is part of recovery
                return self._fail(supervisor, f"history seeding failed: {exc}", started)
        generation = int(getattr(supervisor, "generation", 0) or 0)
        if supervisor is not None:
            supervisor.recovery_succeeded(
                last_event_at=last_event_at, recovered_through=recovered_through
            )
        duration = max(0.0, time.monotonic() - started)
        self._record_success(duration, missing)
        if self._bus is not None:
            self._bus.publish(
                FeedRecovered(
                    generation=generation,
                    recovered_through=recovered_through,
                    instruments=names,
                    bars_replayed=len(recovered),
                    missing_bars=missing,
                )
            )
        log.info(
            "feed recovery complete: generation=%s instruments=%d bars=%d missing=%d",
            generation,
            len(names),
            len(recovered),
            missing,
        )
        return RecoveryOutcome(
            ok=True,
            generation=generation,
            instruments=names,
            bars_replayed=len(recovered),
            missing_bars=missing,
            recovered_through=recovered_through,
            duration_seconds=duration,
        )

    # -- internals ----------------------------------------------------------

    def _subscribed_instruments(self) -> tuple[Any, ...]:
        snapshot = getattr(self._feed, "snapshot_instruments", None)
        if callable(snapshot):
            try:
                return tuple(snapshot())
            except Exception:  # noqa: BLE001 - treat as nothing to recover
                log.warning("feed instrument snapshot failed", exc_info=True)
        return ()

    def _start_for(
        self, inst: Any, last_event_at: datetime | None, end: datetime
    ) -> datetime:
        bar = self.last_closed_bar(getattr(inst, "instrument_id", inst))
        epoch = _bar_epoch(bar) if bar is not None else None
        if epoch is not None:
            return datetime.fromtimestamp(epoch, tz=UTC)
        if isinstance(last_event_at, datetime):
            return last_event_at
        return end - timedelta(seconds=self._lookback_seconds)

    def _dedupe(self, bars: Any) -> list[Any]:
        seen: set[tuple[str, str, int]] = set()
        out: list[Any] = []
        for bar in bars or ():
            epoch = _bar_epoch(bar)
            if epoch is None:
                continue
            key = (_bar_instrument(bar), _bar_timeframe(bar, self._timeframe), epoch)
            if key in seen:
                continue
            seen.add(key)
            out.append(bar)
        return out

    def _missing_between(
        self, inst: Any, bars: list[Any], last_event_at: datetime | None
    ) -> int:
        if not bars:
            return 0
        # An unsupported timeframe means no grid to count gaps on — skip the
        # count (the old private helper's ``None`` path) rather than fail a
        # recovery that history itself may have made continuous.
        try:
            step = interval_seconds(self._timeframe)
        except ValueError:
            return 0
        epochs = sorted({e for e in (_bar_epoch(b) for b in bars) if e is not None})
        if not epochs:
            return 0
        missing = 0
        anchor_epoch: int | None = None
        bar = self.last_closed_bar(getattr(inst, "instrument_id", inst))
        if bar is not None:
            anchor_epoch = _bar_epoch(bar)
        if anchor_epoch is None and isinstance(last_event_at, datetime):
            anchor_epoch = int(last_event_at.timestamp()) // step * step
        if anchor_epoch is not None:
            missing += max(0, (epochs[0] - anchor_epoch) // step - 1)
        for previous, nxt in zip(epochs, epochs[1:]):
            missing += max(0, (nxt - previous) // step - 1)
        return missing

    def _fail(
        self, supervisor: FeedSupervisor | None, message: str, started: float
    ) -> RecoveryOutcome:
        if supervisor is not None:
            supervisor.recovery_failed(message)
        duration = max(0.0, time.monotonic() - started)
        if self._metrics is not None:
            self._metrics.counter("feed_recovery_failures_total").inc()
            self._metrics.histogram("feed_resync_duration_seconds").observe(duration)
        log.warning("feed recovery failed: %s", message)
        return RecoveryOutcome(
            ok=False,
            generation=int(getattr(supervisor, "generation", 0) or 0),
            error=message,
            duration_seconds=duration,
        )

    def _record_success(self, duration: float, missing: int) -> None:
        if self._metrics is None:
            return
        self._metrics.histogram("feed_resync_duration_seconds").observe(duration)
        if missing:
            self._metrics.counter("feed_gaps_total").inc(missing)


class ParquetHistoryProvider:
    """``HistoryProvider`` backed by the local parquet datalake.

    The datalake is the only history source the process already owns for live
    mode (the REST history endpoints are rate limited and user driven), so a
    reconnect replays from locally stored closed bars. Imported lazily so
    importing this module never drags the datalake stack in.
    """

    def __init__(self, store: Any | None = None) -> None:
        self._store = store

    def fetch_bars(
        self,
        instrument: Any,
        timeframe: str,
        start: datetime,
        end: datetime,
    ) -> tuple[Any, ...]:
        from tradex_market_data.market_provider import ParquetMarketProvider
        from tradex_market_data.parquet_storage import ParquetStorage
        from tradex_market_data.paths import DATALAKE_ROOT
        from tradex_market_data.window_loader import MarketWindowLoader

        store = self._store if self._store is not None else ParquetStorage(
            DATALAKE_ROOT, metrics=self._metrics
        )
        # Use MarketWindowLoader as the canonical data loading entry point.
        loader = MarketWindowLoader(provider=ParquetMarketProvider(store=store))
        series = loader.history(instrument, Timeframe(str(timeframe)), start, end)
        return tuple(getattr(series, "candles", ()) or ())


__all__ = [
    "FeedRecoveryCoordinator",
    "HistoryProvider",
    "ParquetHistoryProvider",
    "RecoveryOutcome",
]

