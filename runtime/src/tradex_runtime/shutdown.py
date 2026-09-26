"""Ordered graceful-shutdown coordinator (PE-10).

Ensures components shut down in the correct order: strategies first
(they produce no new events), then the execution engine (drains the
pipeline), then the broker (closes transports), then the session
(releases resources), and finally the writer lock.

Each phase runs in its own try/except so one component's failure does
not prevent the remaining phases from executing.
"""

from __future__ import annotations

import logging
from typing import Callable, NamedTuple

log = logging.getLogger(__name__)


class _ShutdownPhase(NamedTuple):
    name: str
    priority: int
    action: Callable[[], None]


class ShutdownCoordinator:
    """Register shutdown phases with priority; lower runs first.

    Usage::

        coord = ShutdownCoordinator()
        coord.register("strategies", priority=1, action=strategy_engine.dispose_all)
        coord.register("engine", priority=2, action=engine.shutdown)
        coord.register("broker", priority=3, action=broker.close)
        coord.register("session", priority=4, action=session.stop)
        coord.register("writer_lock", priority=5, action=writer_lock.release)

        coord.shutdown()  # runs all phases in priority order
    """

    def __init__(self) -> None:
        self._phases: list[_ShutdownPhase] = []

    def register(
        self,
        name: str,
        *,
        priority: int,
        action: Callable[[], None],
    ) -> None:
        """Register a shutdown phase. Lower priority runs first."""
        self._phases.append(_ShutdownPhase(name=name, priority=priority, action=action))
        self._phases.sort(key=lambda p: p.priority)

    def shutdown(self) -> list[str]:
        """Execute all registered phases in priority order.

        Returns a list of phase names that raised an exception (empty
        when all phases succeed). Each failure is logged but does not
        prevent subsequent phases from running.
        """
        failures: list[str] = []
        for phase in self._phases:
            try:
                phase.action()
                log.debug("shutdown phase %r completed", phase.name)
            except Exception as exc:
                log.error("shutdown phase %r failed: %s", phase.name, exc, exc_info=True)
                failures.append(phase.name)
        return failures
