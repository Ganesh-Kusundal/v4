"""Compatibility shim — implementation lives in ``tradex_observability.metrics``.

``MetricsRegistry`` is the public metric registry; ``_Counter``/``_Gauge``/
``_Histogram`` are the metric handle types, re-exported here because
``trading/tests/runtime/test_v3_port_gaps.py`` asserts on the counter/gauge/
histogram ports the registry hands back. These were previously forwarded
dynamically, via a ``globals().update(...)`` copy that mypy cannot see through.
The explicit re-exports preserve every name the copy provided, so the test's
imports keep working, and are visible to mypy.
"""

from tradex_observability.metrics import (
    MetricsRegistry,
    _Counter,
    _Gauge,
    _Histogram,
)

__all__ = ["MetricsRegistry", "_Counter", "_Gauge", "_Histogram"]
