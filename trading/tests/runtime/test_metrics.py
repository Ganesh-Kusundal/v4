"""Tests for MetricsRegistry — counter, gauge, snapshot, reset."""

from __future__ import annotations

from tradex_trading.runtime.metrics import MetricsRegistry


def test_metrics_registry_counter_and_gauge() -> None:
    """Counter increments and gauge sets values correctly."""
    reg = MetricsRegistry()
    c = reg.counter("orders.filled")
    c.inc()
    c.inc(4)
    assert c.value() == 5.0
    g = reg.gauge("positions.open")
    g.set(10.0)
    assert g.value() == 10.0
    g.set(3.0)
    assert g.value() == 3.0


def test_metrics_registry_snapshot() -> None:
    """Snapshot returns all registered metrics."""
    reg = MetricsRegistry()
    reg.counter("a").inc(2)
    reg.gauge("b").set(7)
    snap = reg.snapshot()
    assert snap["a"] == 2.0
    assert snap["b"] == 7.0


def test_metrics_registry_reset_clears_all() -> None:
    """Reset clears all counters, gauges, histograms, and legacy store."""
    reg = MetricsRegistry()
    reg.counter("x").inc(10)
    reg.gauge("y").set(5)
    reg.histogram("z").observe(1.5)
    reg.reset()
    snap = reg.snapshot()
    assert snap == {}
    # After reset, creating the same counter starts fresh
    assert reg.counter("x").value() == 0.0
