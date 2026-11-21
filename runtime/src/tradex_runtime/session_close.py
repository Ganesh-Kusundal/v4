"""The caller that closes the session's open bars at 15:30 IST.

``BarAggregator.flush`` is documented as the composition root's to call at the
cash close — "the caller does this at 15:30 or on teardown" — and no caller
did the 15:30 half. A bucket left open by the day's last tick therefore never
closed: no ``Candle`` for 15:29-15:30 was ever published, so an intraday
strategy had no closed bar on which to exit, and the parity suite's end-of-day
assertion (A5) sat behind a strict xfail. This is that missing caller, in the
same shape as the missing bar bridge (F-39) one layer down.

The clock is injected and the due-check is a plain method, so the decision is
tested without waiting for 15:30. The thread only sleeps between checks.
"""

from __future__ import annotations

import logging
import threading
from collections.abc import Callable
from datetime import date, datetime, time
from typing import Any

from tradex_domain.market_calendar import IST, MARKET_CLOSE

log = logging.getLogger(__name__)


class SessionCloseFlush:
    """Call *flush* once per IST calendar day, at or after the cash close.

    Once a day, not once per poll: a second flush after the bars are closed
    would be a no-op today, but a retry loop that re-fired on every poll would
    hammer a callback whose failure has already been logged. The date is
    recorded only after *flush* returns, so a failed close is retried on the
    next poll rather than silently skipped for the day.

    Args:
        flush: the close action (``SessionBarAggregator.flush``).
        now: returns the current time; must be IST-aware or it will be
            converted. ``datetime.now(IST)`` by default.
        close: the wall-clock close to fire at. ``MARKET_CLOSE`` by default.
        poll_seconds: how long to sleep between due-checks.
    """

    def __init__(
        self,
        flush: Callable[[], Any],
        *,
        now: Callable[[], datetime] | None = None,
        close: time = MARKET_CLOSE,
        poll_seconds: float = 30.0,
    ) -> None:
        self._flush = flush
        self._now = now or (lambda: datetime.now(IST))
        self._close = close
        self._poll = float(poll_seconds)
        self._last_close: date | None = None
        self._stop = threading.Event()
        self._thread: threading.Thread | None = None

    # -- the decision (public so it can be tested without a thread) --------

    def flush_if_due(self) -> bool:
        """Flush if today's close has passed and today has not closed yet.

        Returns whether the flush ran. A raising *flush* propagates: the
        caller (the thread loop) logs and retries on the next poll.
        """
        now = self._now()
        if now.tzinfo is not None:
            now = now.astimezone(IST)
        today = now.date()
        if today == self._last_close:
            return False
        if now.time() < self._close:
            return False
        self._flush()
        self._last_close = today
        log.info(
            "session close flush ran at %s IST",
            now.strftime("%H:%M:%S"),
            extra={"stage": "session.close_flush"},
        )
        return True

    # -- the thread --------------------------------------------------------

    def start(self) -> None:
        """Begin polling. Idempotent."""
        if self._thread is not None:
            return
        self._stop.clear()
        self._thread = threading.Thread(
            target=self._run, name="session-close-flush", daemon=True
        )
        self._thread.start()

    def stop(self) -> None:
        """Stop polling and join. Idempotent and safe before ``start``."""
        self._stop.set()
        thread, self._thread = self._thread, None
        if thread is not None:
            thread.join(timeout=max(1.0, self._poll))

    def _run(self) -> None:
        while not self._stop.wait(self._poll):
            try:
                self.flush_if_due()
            except Exception:  # pragma: no cover - defensive; retried next poll
                log.warning("session close flush failed", exc_info=True)


__all__ = ["SessionCloseFlush"]
