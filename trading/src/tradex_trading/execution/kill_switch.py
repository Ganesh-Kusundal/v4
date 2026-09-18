"""Kill switch — halt new submissions and cancel every open order.

Candidate 1 of the 2026-09-17 architecture review asked for the kill switch to
leave ``engine.py`` and live in its own module, the way fees and fill dedup
already did. It was a method on ``ExecutionEngine`` that reached into the
metrics registry, the risk manager's master gate and the order cache, and the
engine is the wrong place to learn what "halt everything" means.

The switch is a ``threading.Event`` with three readers: the pipeline filter
(drops new submissions while it is set), the order intake (rejects with
``kill_switch_active``), and the shutdown path. Keeping the event here means a
future reader finds the halt logic in one module instead of three methods.
"""

from __future__ import annotations

import logging
import threading
from typing import TYPE_CHECKING, Any

from tradex_domain.enums import OrderStatus
from tradex_domain.value_objects import OrderId

if TYPE_CHECKING:
    from tradex_trading.execution.risk import RiskManager
    from tradex_trading.execution.trading_cache import TradingCache
    from tradex_trading.runtime.metrics import MetricsRegistry

log = logging.getLogger(__name__)

#: An order in one of these states is done; the kill switch does not cancel it.
TERMINAL_STATUSES = frozenset({
    OrderStatus.FILLED,
    OrderStatus.CANCELLED,
    OrderStatus.REJECTED,
    OrderStatus.UNKNOWN,
})


class KillSwitch:
    """Halt-on-trip master gate for an execution engine.

    ``trip`` sets the flag, disables the risk manager's live-order gate and
    cancels every non-terminal order through the caller's own cancel path —
    which is deliberately a callback, not an import: ``ExecutionEngine.cancel``
    dispatches the venue for plain orders and the bracket endpoint for
    brackets, and a separate venue call here would cancel the venue twice.
    """

    def __init__(
        self,
        *,
        metrics: "MetricsRegistry | None" = None,
        risk: "RiskManager | None" = None,
        cache: "TradingCache | None" = None,
    ) -> None:
        self._event = threading.Event()
        self._metrics = metrics
        self._risk = risk
        self._cache = cache

    # ------------------------------------------------------------------ state

    def is_set(self) -> bool:
        """Whether new submissions are currently halted."""
        return self._event.is_set()

    def set(self) -> None:
        """Halt new submissions. Idempotent."""
        self._event.set()

    def clear(self) -> None:
        """Resume submissions."""
        self._event.clear()

    @property
    def active(self) -> bool:
        """Read-only view of the gate, for the pipeline filter and intake.

        Delegates to :meth:`is_set` rather than touching the event directly, so
        a caller (or test) that patches the method sees the change here too.
        """
        return self.is_set()

    @active.setter
    def active(self, value: bool) -> None:
        """Activate or deactivate the switch (engine property back-compat)."""
        if value:
            self.set()
        else:
            self.clear()

    # ------------------------------------------------------------------ trip

    def trip(
        self,
        reason: str = "",
        *,
        cancel: "Any | None" = None,
    ) -> list[str]:
        """Halt new submissions and cancel every open order.

        Returns the order ids that could not be cancelled. ``cancel`` is the
        engine's own cancel callable, taking an ``OrderId`` and returning the
        cancelled order; the venue dispatch stays with the engine.
        """
        log.critical("Kill switch tripped: %s", reason)
        if self._metrics is not None:
            self._metrics.counter("kill_switch.tripped").inc()
        self.set()
        # Propagate to risk manager master gate
        if self._risk is not None:
            self._risk.live_orders_enabled = False

        if cancel is None or self._cache is None:
            return []
        failures: list[str] = []
        for order in self._cache.all_orders():
            if order.status in TERMINAL_STATUSES:
                continue
            try:
                cancel(order.order_id)
            except Exception as exc:  # noqa: BLE001 — one failure must not stop the rest
                log.error("kill-switch cancel failed for %s: %s", order.order_id, exc)
                failures.append(order.order_id.value)
        return failures


__all__ = ["KillSwitch", "TERMINAL_STATUSES"]
