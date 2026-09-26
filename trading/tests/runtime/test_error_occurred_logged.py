"""H7 — ErrorOccurred subscriber.

The principal-architect review (H7) found that
``engine.py:517, 524, 533`` publish ``ErrorOccurred`` on the bus but
no code subscribes. Pipeline errors are silently dropped. The fix:
in the boot path, subscribe a logger + a counter to ``ErrorOccurred``
so operators see pipeline failures and the runtime increments a
metric.
"""

from __future__ import annotations

from tradex_domain.events import ErrorOccurred

from tradex_trading.config.schema import AppConfig
from tradex_trading.runtime import startup as startup_mod


def test_error_occurred_counter_increments_after_publish() -> None:
    """RED: after a paper boot, publishing ErrorOccurred increments
    ``engine.errors.total`` (or equivalent). The principal-architect
    fix wires a subscriber that logs + counts.
    """
    cfg = AppConfig(mode="paper")
    session = startup_mod.boot(cfg)
    try:
        engine = session.engine
        metrics = engine._metrics  # type: ignore[attr-defined]
        # The boot must wire a subscriber that increments a counter.
        # Pre-fix: no counter exists. Post-fix: counter at a known
        # name (the bus errors total) increments on every ErrorOccurred
        # published.
        before = metrics.counter("engine.errors.total").value()
        session.bus.publish(ErrorOccurred(error=RuntimeError("test")))
        after = metrics.counter("engine.errors.total").value()
        assert after > before, (
            "ErrorOccurred must increment engine.errors.total counter (H7)"
        )
    finally:
        session.stop()
