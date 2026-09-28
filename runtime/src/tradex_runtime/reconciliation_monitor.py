"""Automatic reconciliation: drift reporting that *adopts* the venue's positions.

``ExecutionEngine.reconcile`` is side-effect free by design — it compares the local
book to the venue's and returns :class:`DriftItem`\\ s, and the caller decides what
to do. That is correct for a report, and it is why nothing ever fixed the book: a
strategy that filled at the venue while the local book lagged stayed wrong
forever, because the only object that knew the answer was the drift item nobody
acted on. The venue's figures were compared and discarded.

:class:`ReconciliationMonitor` closes that loop. It owns a bounded daemon thread —
the same lifecycle :class:`~tradex_runtime.feed_monitor.FeedStaleMonitor` uses —
that reconciles on a timer, reports drift exactly as before, and additionally
*adopts* the venue's positions into the book when it is safe to.

The adoption is the new behaviour and it is the dangerous one, so it is gated by
the venue-read contract in :mod:`tradex_interfaces.safety`:

* **A failed read never adopts.** ``broker.get_positions()`` raising produces a
  *failed* :class:`VenueRead`, and a failed read carries no positions at all, so
  adopting from it is not expressible. The monitor counts the failure and moves
  on; the book is untouched.
* **A partial read never adopts.** A venue that answers with gaps is refused
  rather than interpreted, because the unread instruments would be read as flat —
  which is the corruption this monitor exists to prevent.
* **A stale read never adopts.** The monitor keeps the stamp of its last
  successful read and refuses one that arrives older, so a retried snapshot that
  lands out of order cannot move the book backwards.
* **An unsettled fill is never adopted over.** An instrument the book believes is
  mid-flight is skipped, because the venue's snapshot cannot yet have seen the
  fill and adopting would undo it — silently, while logging a "correction".

Why the safety surface owns the read and this module owns the timer
-------------------------------------------------------------------
``tradex_runtime`` may not import ``tradex_interfaces`` (``tests/test_import_boundaries.py``
pins it), and the direction is the right one: ``interfaces`` is the layer that
sees the execution book *and* the analytics promotion bar, so the join belongs
there, not here. So the adoption logic — the read, the plan, the refusal — is
defined in :mod:`tradex_interfaces.safety` and this module *calls* it through the
two callables it is handed at construction. Nothing is duplicated: the same
:func:`tradex_interfaces.safety.adopt_venue_positions` the monitor calls is the one
the HTTP surface calls for a dry-run, so a "what would change" shown to an
operator and a change written by the timer cannot disagree.
"""

from __future__ import annotations

import logging
import threading
from collections.abc import Callable, Sequence
from datetime import UTC, datetime
from typing import Any

log = logging.getLogger(__name__)

#: Reconcile this often. The venue's own position stream pushes updates, so this
#: poll is the *correction* path, not the delivery path: it exists to catch the
#: drift a missed push left behind. Twice a minute is fast enough that a fill the
#: book missed is corrected within one bar, and slow enough that it does not
#: interrogate the venue's rate-limited positions endpoint on every tick.
RECONCILIATION_INTERVAL_SECONDS = 30.0

#: Metrics emitted per poll. The drift counter follows the existing convention
#: (``reconciliation_drift_total.<severity>.<kind>`` in
#: ``deploy/prometheus-rules.yml``) and is emitted by the injected adopter, which
#: is the same code path the safety route reports from. The adoption metrics are
#: new with this module.
#:
#: These are declared as literals rather than imported from
#: :mod:`tradex_interfaces.safety` because ``tradex_runtime`` may not import
#: ``tradex_interfaces`` (pinned by ``tests/test_import_boundaries.py``). A metric
#: name is a wire contract with Prometheus, not a code dependency: both modules
#: declare it and ``trading/tests/runtime/test_reconciliation_monitor.py`` pins
#: that they agree, which is what makes a drift a visible thing rather than a
#: silent one.
_METRIC_ADOPTED = "venue_adoption_adopted_total"
_METRIC_CLEARED = "venue_adoption_cleared_total"
_METRIC_SKIPPED = "venue_adoption_skipped_total"
_METRIC_REFUSED = "venue_adoption_refused_total"
_METRIC_READ_FAILED = "venue_read_failed_total"
_METRIC_LAST_ADOPTED = "venue_adoption_last_adopted"


class ReconciliationMonitor:
    """Owns the periodic reconcile-and-adopt poll for one trading session.

    The monitor holds three injected seams and no domain knowledge of its own:

    ``reconcile``
        Runs the drift comparison and returns the drift items. Usually bound to
        ``engine.reconcile``. Never called when omitted.
    ``adopt``
        The adoption itself: reads the venue, refuses a bad read, derives the
        plan, applies it to the cache and returns an outcome describing what
        changed. Bound to :func:`tradex_interfaces.safety.adopt_venue_positions` by
        the composition root — the same function the safety route calls for a
        dry-run, so an operator's "what would change" and the timer's change are
        the same code path.
    ``metrics``
        Optional ``MetricsRegistry``.

    Both seams are wrapped so a monitor can never die from them, and the two are
    called in one order on every poll: reconcile first (the book's own state is
    the input), then adopt (the correction). The adoption is *also* the thing that
    reports drift now, because an adoption that found nothing to change is the
    strongest possible reading of "no drift".
    """

    def __init__(
        self,
        *,
        reconcile: Callable[[], Sequence[Any]] | None = None,
        adopt: Callable[[], Any] | None = None,
        metrics: Any | None = None,
        interval_seconds: float = RECONCILIATION_INTERVAL_SECONDS,
        clock: Callable[[], datetime] | None = None,
    ) -> None:
        if interval_seconds <= 0:
            raise ValueError("interval_seconds must be positive")
        self._reconcile_fn = reconcile
        self._adopt_fn = adopt
        self._metrics = metrics
        self._interval = float(interval_seconds)
        self._clock = clock or (lambda: datetime.now(UTC))
        self._thread: threading.Thread | None = None
        self._stop = threading.Event()
        self._lock = threading.Lock()
        #: The drift items from the most recent reconciliation, so a caller can
        #: state what was found rather than merely that the poll ran.
        self._last_drifts: tuple[Any, ...] = ()
        #: The last adoption outcome. ``None`` means no adopter was given.
        self._last_adoption: Any | None = None

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
        """Start the poll loop. Idempotent — never spawns a second timer.

        The guard is ``self._thread`` being live, not merely non-``None``: a dead
        thread is still an object, and a monitor whose thread died on an unhandled
        error must be restartable rather than refused by its own corpse. Guards on
        ``self.running`` instead would leave a crashed monitor unrestartable for
        the rest of the session.
        """
        with self._lock:
            if self._thread is not None and self._thread.is_alive():
                return
            self._stop.clear()
            thread = threading.Thread(
                target=self._run,
                name="reconciliation-monitor",
                daemon=True,
            )
            self._thread = thread
            thread.start()

    def stop(self, timeout_seconds: float = 5.0) -> None:
        """Signal the poll loop to exit and join it. Idempotent.

        Clears :attr:`_thread` under the lock *before* joining, so a concurrent
        ``start()`` sees a dead monitor as stopped rather than as a live thread to
        be refused by, and the join cannot deadlock with it.
        """
        with self._lock:
            thread = self._thread
            self._thread = None
            self._stop.set()
        if thread is not None and thread.is_alive():
            thread.join(timeout=timeout_seconds)
            if thread.is_alive():  # pragma: no cover - defensive
                log.warning(
                    "reconciliation monitor did not stop within %.1fs",
                    timeout_seconds,
                )

    def __enter__(self) -> ReconciliationMonitor:
        self.start()
        return self

    def __exit__(self, *args: object) -> None:
        self.stop()

    # -- introspection -------------------------------------------------------

    @property
    def last_drifts(self) -> tuple[Any, ...]:
        """The drift items from the most recent reconciliation."""
        with self._lock:
            return self._last_drifts

    @property
    def last_adoption(self) -> Any | None:
        """The most recent adoption outcome, or ``None`` if it never adopted."""
        with self._lock:
            return self._last_adoption

    @property
    def adopted_count(self) -> int:
        """Instruments the last adoption actually wrote.

        Reads the outcome's own ``adopted_count`` rather than the ``applied`` flag:
        a refused adoption is still an adoption an operator needs the size of. A
        missing outcome or a missing attribute is 0, never a raise.
        """
        with self._lock:
            adoption = self._last_adoption
        if adoption is None:
            # No adoption has run yet, or the seam raised: the count is 0 rather
            # than a raise, because a gauge that explodes is worse than an absent one.
            return 0
        try:
            return int(getattr(adoption, "adopted_count", 0))
        except (TypeError, ValueError):  # pragma: no cover - defensive
            return 0

    # -- polling -------------------------------------------------------------

    def poll_once(self) -> tuple[tuple[Any, ...], Any | None]:
        """Run one bounded reconciliation; return the drift items and the adoption.

        Never raises. This is the single load-bearing method: it is what the owned
        thread calls, what a test drives, and what a caller can invoke directly to
        reconcile on demand. Every failure path degrades to a logged warning and a
        returned empty result rather than an exception, because a monitor that dies
        is a monitor that stopped reconciling — the opposite of the safe direction.
        """
        drifts = self._reconcile()
        adoption = self._adopt() if self._adopt_fn is not None else None
        self._publish_metrics(drifts, adoption)
        return drifts, adoption

    # -- internals -----------------------------------------------------------

    def _run(self) -> None:
        while not self._stop.wait(self._interval):
            try:
                self.poll_once()
            except Exception:  # pragma: no cover - defensive belt
                log.exception("reconciliation monitor poll failed")

    def _reconcile(self) -> tuple[Any, ...]:
        """Run the drift comparison. Never raises."""
        reconcile = self._reconcile_fn
        if reconcile is None:
            with self._lock:
                self._last_drifts = ()
            return ()
        try:
            drifts = tuple(reconcile())
        except Exception:  # noqa: BLE001 - monitoring must not crash
            log.warning("reconciliation comparison failed", exc_info=True)
            with self._lock:
                self._last_drifts = ()
            return ()
        with self._lock:
            self._last_drifts = drifts
        return drifts

    def _adopt(self) -> Any:
        """Run the adoption seam, or record the failure. Never raises."""
        adopt = self._adopt_fn
        try:
            outcome = adopt()
        except Exception:  # noqa: BLE001 - monitoring must not crash
            log.warning("venue position adoption failed", exc_info=True)
            return None
        with self._lock:
            self._last_adoption = outcome
        self._log_outcome(outcome)
        return outcome

    def _log_outcome(self, outcome: Any) -> None:
        """Log the adoption, per instrument it acted on and once for the verdict."""
        if outcome is None:
            return
        try:
            self._write_outcome_log(outcome)
        except Exception:  # noqa: BLE001 - a log must never kill the poll
            log.warning("reconciliation adoption log failed", exc_info=True)

    def _write_outcome_log(self, outcome: Any) -> None:
        applied = bool(getattr(outcome, "applied", False))
        read = getattr(outcome, "read", None)
        if not applied:
            log.warning(
                "venue position adoption refused: %s (read=%s)",
                getattr(outcome, "refusal", ""),
                read.as_log_dict() if read is not None else None,
            )
            return
        plan = getattr(outcome, "adoption", None)
        if plan is None or bool(getattr(plan, "is_empty", False)):
            log.info(
                "venue position adoption: no changes (venue=%d positions)",
                len(read.positions) if read is not None else 0,
            )
            return
        for row in plan.as_log_rows():
            log.warning("venue position adoption: %s", row)
        log.warning(
            "venue position adoption complete: adopted=%d retained=%d cleared=%d "
            "skipped=%d",
            len(plan.positions),
            len(plan.retained),
            len(plan.cleared),
            len(plan.skipped),
        )

    def _publish_metrics(
        self,
        drifts: Sequence[Any],
        adoption: Any | None,
    ) -> None:
        """Emit the counters and gauges for one poll.

        The drift counters are the adopter's business to emit (it is the code path
        that already derives severity and owns the label convention); this module
        emits only the adoption metrics, which are its own. Splitting it this way
        keeps one metric family owned by one module — a counter incremented in two
        places is a counter that can disagree with itself.

        Never raises. A metrics backend that dies is a hole in the graph, and a
        hole is what a logged warning is for; letting it escape would cost the
        reconciliation itself, which is strictly worse.
        """
        metrics = self._metrics
        if metrics is None or adoption is None:
            return
        try:
            self._write_metrics(metrics, adoption)
        except Exception:  # noqa: BLE001 - metrics must not kill the poll
            log.warning("reconciliation metrics publish failed", exc_info=True)

    def _write_metrics(self, metrics: Any, adoption: Any) -> None:
        read = getattr(adoption, "read", None)
        if read is not None and bool(getattr(read, "failed", False)):
            metrics.counter(_METRIC_READ_FAILED).inc()
        plan = getattr(adoption, "adoption", None)
        skipped = dict(getattr(plan, "skipped", {}) or {})
        if bool(getattr(adoption, "applied", False)):
            metrics.counter(_METRIC_ADOPTED).inc(float(self._len(plan, "positions")))
            metrics.counter(_METRIC_CLEARED).inc(float(self._len(plan, "cleared")))
            metrics.gauge(_METRIC_LAST_ADOPTED).set(
                float(self._len(plan, "positions")),
            )
        else:
            refusal = getattr(adoption, "refusal", "")
            if refusal:
                metrics.counter(_METRIC_REFUSED).inc(1, reason=str(refusal))
            # A refused read still reports what it would have skipped, because a
            # refusal that hides its reasons is a refusal an operator cannot clear.
            for reason in frozenset(skipped.values()):
                metrics.counter(_METRIC_SKIPPED).inc(
                    float(sum(1 for value in skipped.values() if value == reason)),
                    reason=str(reason),
                )
            return
        for reason in frozenset(skipped.values()):
            metrics.counter(_METRIC_SKIPPED).inc(
                float(sum(1 for value in skipped.values() if value == reason)),
                reason=str(reason),
            )

    @staticmethod
    def _len(plan: Any, name: str) -> int:
        """The size of a plan bucket, ``0`` when the plan or bucket is absent."""
        if plan is None:
            return 0
        bucket = getattr(plan, name, None) or ()
        return len(bucket)


__all__ = [
    "RECONCILIATION_INTERVAL_SECONDS",
    "ReconciliationMonitor",
]
