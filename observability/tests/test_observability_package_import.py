"""Smoke + metrics registry behavior."""

from tradex_observability import MetricsRegistry


def test_import_package() -> None:
    assert MetricsRegistry is not None


def test_counter_inc() -> None:
    reg = MetricsRegistry()
    c = reg.counter("t")
    c.inc()
    assert c.value() == 1.0
