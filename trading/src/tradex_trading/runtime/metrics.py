"""Simple metrics registry for tracking counters, gauges, and histograms.

Provides basic metrics collection for monitoring and diagnostics.
Stdlib-only — no external Prometheus dependency.
"""

from __future__ import annotations

import re
import threading


class _Counter:
    """Monotonically increasing counter."""

    def __init__(self, name: str, help_text: str = "") -> None:
        self._name = name
        self._help = help_text
        self._value = 0.0
        # H1: lock makes the read-modify-write atomic across threads.
        self._lock = threading.Lock()

    def inc(self, amount: float = 1, **labels: object) -> None:
        del labels
        with self._lock:
            self._value += amount

    def value(self) -> float:
        with self._lock:
            return self._value


class _Gauge(_Counter):
    """Value that can go up and down."""

    def set(self, value: float, **labels: object) -> None:
        del labels
        with self._lock:
            self._value = value


class _Histogram:
    """Cumulative histogram with count, min, max, and sum tracking."""

    def __init__(self, name: str, help_text: str = "") -> None:
        self._name = name
        self._help = help_text
        self._sum = 0.0
        self._count = 0
        self._min = float("inf")
        self._max = float("-inf")
        # H1: lock for the same reason as _Counter.
        self._lock = threading.Lock()

    def observe(self, amount: float, **labels: object) -> None:
        del labels
        with self._lock:
            self._sum += amount
            self._count += 1
            if amount < self._min:
                self._min = amount
            if amount > self._max:
                self._max = amount

    def value(self) -> float:
        with self._lock:
            return self._sum

    @property
    def count(self) -> int:
        with self._lock:
            return self._count

    @property
    def min(self) -> float:
        with self._lock:
            return self._min if self._count > 0 else 0.0

    @property
    def max(self) -> float:
        with self._lock:
            return self._max if self._count > 0 else 0.0


class MetricsRegistry:
    """Simple metrics collection.

    Thread-safe counter/gauge/histogram registry for tracking operational
    metrics.
    """

    def __init__(self) -> None:
        self._counters: dict[str, _Counter] = {}
        self._gauges: dict[str, _Gauge] = {}
        self._histograms: dict[str, _Histogram] = {}

    # -- rich metric API --------------------------------------------------------

    def counter(self, name: str, help_text: str = "") -> _Counter:
        """Return (or create) a named counter."""
        metric = self._counters.get(name)
        if metric is None:
            metric = _Counter(name, help_text)
            self._counters[name] = metric
        return metric

    def gauge(self, name: str, help_text: str = "") -> _Gauge:
        """Return (or create) a named gauge."""
        metric = self._gauges.get(name)
        if metric is None:
            metric = _Gauge(name, help_text)
            self._gauges[name] = metric
        return metric

    def histogram(self, name: str, help_text: str = "") -> _Histogram:
        """Return (or create) a named histogram."""
        metric = self._histograms.get(name)
        if metric is None:
            metric = _Histogram(name, help_text)
            self._histograms[name] = metric
        return metric

    def get(self, name: str) -> float:
        """Get the current value of any named metric.

        Searches rich counters, gauges, histograms. Returns 0 for unknown
        metrics.
        """
        for bucket in (self._counters, self._gauges, self._histograms):
            if name in bucket:
                return bucket[name].value()
        return 0

    def snapshot(self) -> dict[str, float]:
        """Get a snapshot of all metrics."""
        out: dict[str, float] = {}
        for bucket in (self._counters, self._gauges, self._histograms):
            for name, metric in bucket.items():
                out[name] = metric.value()
        return out

    def reset(self) -> None:
        """Reset all metrics."""
        self._counters.clear()
        self._gauges.clear()
        self._histograms.clear()

    def render_prometheus(self) -> str:
        """Render all metrics in Prometheus text exposition format."""

        def _sanitize(name: str) -> str:
            return re.sub(r"[^a-zA-Z0-9_:]", "_", name)

        lines: list[str] = []
        for name, c in sorted(self._counters.items()):
            s = _sanitize(name)
            lines.append(f"# TYPE {s} counter")
            lines.append(f"{s} {c.value()}")
        for name, g in sorted(self._gauges.items()):
            s = _sanitize(name)
            lines.append(f"# TYPE {s} gauge")
            lines.append(f"{s} {g.value()}")
        for name, h in sorted(self._histograms.items()):
            s = _sanitize(name)
            lines.append(f"# TYPE {s} summary")
            lines.append(f"{s}_count {h.count}")
            lines.append(f"{s}_sum {h.value()}")
            lines.append(f"{s}_min {h.min}")
            lines.append(f"{s}_max {h.max}")
        return "\n".join(lines) + ("\n" if lines else "")


__all__ = ["MetricsRegistry"]
