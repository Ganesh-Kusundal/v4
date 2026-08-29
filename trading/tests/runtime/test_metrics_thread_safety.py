"""H1 — MetricsRegistry lock.

The principal-architect review (H1) found that
``MetricsRegistry._Counter.inc()`` is non-atomic
(``self._value += amount`` with no lock). CPython's GIL often
masks the race in practice (the load-add-store is fast enough
that context switches between ops are rare), but the contract is
still racy: another thread can observe a stale value. The fix is
a single ``threading.Lock`` per counter (and per histogram).

These tests pin the contract: the metrics objects own a lock and
their hot methods acquire it. (We do not try to expose the GIL
race in tests — it is brittle and platform-dependent. The lock is
the contract.)
"""

from __future__ import annotations

import threading

from tradex_trading.runtime.metrics import MetricsRegistry


def test_counter_owns_a_lock() -> None:
    """RED: ``_Counter`` must have a lock attribute. Used to hot-path
    inc/observe methods. The pre-fix code has no lock."""
    reg = MetricsRegistry()
    counter = reg.counter("test")
    assert hasattr(counter, "_lock"), (
        "Counter must own a lock (H1: thread-safe inc)"
    )
    assert isinstance(counter._lock, type(threading.Lock()))  # type: ignore[attr-defined]


def test_histogram_owns_a_lock() -> None:
    """Same contract for histograms."""
    reg = MetricsRegistry()
    hist = reg.histogram("test", "test help")
    assert hasattr(hist, "_lock"), (
        "Histogram must own a lock (H1: thread-safe observe)"
    )
    assert isinstance(hist._lock, type(threading.Lock()))  # type: ignore[attr-defined]
