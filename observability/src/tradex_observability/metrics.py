"""Simple metrics registry for tracking counters, gauges, and histograms.

Provides basic metrics collection for monitoring and diagnostics.
Stdlib-only — no external Prometheus dependency.

Every named metric is a *family of series*. A series is identified by its
label set reduced to a sorted tuple of ``(key, value)`` pairs, so label order
never matters: ``{a=1, b=2}`` and ``{b=2, a=1}`` address the same series, while
two different label sets stay two different rows on ``/metrics``.

Counters, gauges and histograms each keep ``dict[LabelKey, _Series]`` guarded
by that metric's own lock, so recording into a labelled series is atomic across
threads and a label set can never silently collapse into another.

Histograms are cumulative Prometheus histograms: ``observe`` fills the bucket
counts, ``render_prometheus`` emits ``_bucket{le=...}``/``_sum``/``_count``, and
``quantile`` reads the buckets back — which is what makes the p95/p99 rows in
``docs/operations/metrics-catalog.md`` computable instead of decorative.
"""

from __future__ import annotations

import math
import re
import threading
from typing import Any

#: Default cumulative histogram bucket upper bounds, in seconds (the
#: Prometheus client default). Chosen to bracket the catalog's latency SLOs —
#: p95 250 ms / p99 750 ms for order submit/process, and the 60 s feed-resync
#: SLO — so ``histogram_quantile`` resolves them without re-bucketing.
DEFAULT_BUCKETS: tuple[float, ...] = (
    0.005,
    0.01,
    0.025,
    0.05,
    0.1,
    0.25,
    0.5,
    1,
    2.5,
    5,
    10,
    30,
    60,
    120,
    300,
)

# ---------------------------------------------------------------------------
# Canonical metric names (REF-13: one home)
# ---------------------------------------------------------------------------
#: The venue-adoption reconciliation family, emitted per poll by the runtime
#: :class:`~tradex_runtime.reconciliation_monitor.ReconciliationMonitor`. A
#: metric name is a wire contract with Prometheus (``deploy/prometheus-rules.yml``
#: reads these), so the string literals live HERE and nowhere else: the runtime
#: monitor imports them to emit, and ``tradex_interfaces.safety`` re-exports them
#: for the safety surface and its tests. Neither consumer may re-declare the
#: string — a drift between two declarations is a silent graph split.
METRIC_ADOPTED = "venue_adoption_adopted_total"
#: Emitted per cleared instrument.
METRIC_CLEARED = "venue_adoption_cleared_total"
#: Emitted per skipped instrument, labelled by reason.
METRIC_SKIPPED = "venue_adoption_skipped_total"
#: Emitted per refused adoption, labelled by reason.
METRIC_REFUSED = "venue_adoption_refused_total"
#: Emitted per failed venue read.
METRIC_READ_FAILED = "venue_read_failed_total"
#: Gauge of how many instruments the last adoption wrote.
METRIC_LAST_ADOPTED = "venue_adoption_last_adopted"

#: A series key: the label set as a sorted tuple of ``(key, value)`` string
#: pairs. Sorting is what makes label order irrelevant.
LabelKey = tuple[tuple[str, str], ...]


def _label_key(labels: dict[str, object]) -> LabelKey:
    """Normalise a label mapping to a sorted, hashable series key.

    Values are stringified so heterogeneous types key the same way they are
    rendered on the wire.
    """
    return tuple(sorted((str(k), str(v)) for k, v in labels.items()))


def _escape(value: str) -> str:
    """Escape a label value for the text exposition format."""
    return value.replace("\\", "\\\\").replace('"', '\\"').replace("\n", "\\n")


def _labels(key: LabelKey) -> str:
    """Render a series key as a Prometheus label set, including the braces.

    An unlabelled series renders as the empty string, not ``{}``: the
    exposition format writes the braces only when there are labels, and
    ``latency_seconds_count 2`` is what existing consumers match on.
    """
    if not key:
        return ""
    return "{" + ",".join(f'{k}="{_escape(v)}"' for k, v in key) + "}"


def _bucket_labels(le: str, key: LabelKey) -> str:
    """Render ``_bucket`` labels with ``le`` first, as Prometheus requires."""
    if not key:
        return f'{{le="{le}"}}'
    inner = ",".join(f'{k}="{_escape(v)}"' for k, v in key)
    return f'{{le="{le}",{inner}}}'


def _bound(bound: float) -> str:
    """Render a bucket bound the way the exposition format spells it."""
    if bound == math.inf:
        return "+Inf"
    return repr(bound)


def _value_text(value: float) -> str:
    """Render a sample value.

    Deliberately plain ``str(float)`` so existing consumers that match on
    ``5.0``/``0.4`` keep matching; only bucket bounds use :func:`_bound`,
    which is the one place ``+Inf`` is a legal literal.
    """
    return str(value)


class _Series:
    """One counter series: a single monotonically increasing float."""

    __slots__ = ("value",)

    def __init__(self) -> None:
        self.value = 0.0


class _GaugeSeries(_Series):
    """One gauge series: a value that can also go down."""


class _HistogramSeries:
    """One histogram series: cumulative bucket counts plus sum/count/min/max."""

    __slots__ = ("bounds", "counts", "count", "sum", "min", "max")

    def __init__(self, bounds: tuple[float, ...]) -> None:
        self.bounds = bounds
        # counts[i] = number of observations <= bounds[i] (cumulative).
        self.counts = [0] * len(bounds)
        self.count = 0
        self.sum = 0.0
        self.min = math.inf
        self.max = -math.inf

    def observe(self, amount: float) -> None:
        self.sum += amount
        self.count += 1
        if amount < self.min:
            self.min = amount
        if amount > self.max:
            self.max = amount
        for i, bound in enumerate(self.bounds):
            if amount <= bound:
                self.counts[i] += 1

    def quantile(self, q: float) -> float:
        """Value at quantile ``q``, interpolated within its bucket.

        This is the reading of ``histogram_quantile`` that ignores a bucket's
        outer bound and interpolates between the bucket's lower and upper edge
        instead, matching ``tradex_trading``'s own off-by-one tolerance rather
        than Prometheus's coarser "assume uniform, take the upper bound" model.
        Both need only the bucket counts, so p95/p99 are computable without
        retaining raw samples.

        Empty series return ``0.0``; an observation above every finite bound
        falls back to the observed max.
        """
        if self.count == 0:
            return 0.0
        if q <= 0:
            return self.min
        if q >= 1:
            return self.max
        rank = math.ceil(q * self.count)
        cumulative = 0
        lower = 0.0
        for i, bound in enumerate(self.bounds):
            bucket_cum = self.counts[i]
            if bucket_cum >= rank:
                in_bucket = bucket_cum - cumulative
                if in_bucket <= 1:
                    return lower if rank < bucket_cum else bound
                frac = (rank - cumulative) / in_bucket
                return lower + (bound - lower) * frac
            cumulative = bucket_cum
            lower = bound
        # Rank sits above the last finite bucket: only the observed max knows.
        return self.max


class _Metric[T]:
    """Base class: a named family of series indexed by its sorted label tuple."""

    def __init__(self, name: str, help_text: str = "") -> None:
        self._name = name
        self._help = help_text
        # H1: the lock makes each read-modify-write atomic across threads and
        # guards the series index, so a labelled record is atomic and two
        # label sets can never collapse into one row.
        self._lock = threading.Lock()
        self._series: dict[LabelKey, T] = {}

    @property
    def name(self) -> str:
        return self._name

    def _sanitized(self) -> str:
        return re.sub(r"[^a-zA-Z0-9_:]", "_", self._name)

    def _render(self) -> list[str]:
        raise NotImplementedError


class _Counter(_Metric[_Series]):
    """Monotonically increasing counter, one series per label set."""

    def inc(self, amount: float = 1, **labels: object) -> None:
        key = _label_key(labels)
        with self._lock:
            series = self._series.get(key)
            if series is None:
                series = _Series()
                self._series[key] = series
            series.value += amount

    def value(self, **labels: object) -> float:
        """This label series' value, or the family total when unlabelled."""
        with self._lock:
            if labels:
                series = self._series.get(_label_key(labels))
                return series.value if series is not None else 0.0
            return sum(s.value for s in self._series.values())

    def _render(self) -> list[str]:
        with self._lock:
            base = self._sanitized()
            return [
                f"# TYPE {base} counter",
                *(
                    f"{base}{_labels(key)} {_value_text(series.value)}"
                    for key, series in sorted(self._series.items())
                ),
            ]


class _Gauge(_Counter):
    """Value that can go up and down, one series per label set."""

    def set(self, value: float, **labels: object) -> None:
        key = _label_key(labels)
        with self._lock:
            series = self._series.get(key)
            if series is None:
                series = _GaugeSeries()
                self._series[key] = series
            series.value = value

    def _render(self) -> list[str]:
        with self._lock:
            base = self._sanitized()
            return [
                f"# TYPE {base} gauge",
                *(
                    f"{base}{_labels(key)} {_value_text(series.value)}"
                    for key, series in sorted(self._series.items())
                ),
            ]


class _Histogram(_Metric[_HistogramSeries]):
    """Cumulative histogram with Prometheus buckets, one series per label set."""

    def __init__(
        self,
        name: str,
        help_text: str = "",
        buckets: tuple[float, ...] = DEFAULT_BUCKETS,
    ) -> None:
        super().__init__(name, help_text)
        self._bounds = tuple(sorted(buckets))

    def observe(self, amount: float, **labels: object) -> None:
        key = _label_key(labels)
        with self._lock:
            series = self._series.get(key)
            if series is None:
                series = _HistogramSeries(self._bounds)
                self._series[key] = series
            series.observe(amount)

    # -- aggregate accessors ----------------------------------------------------
    # ``value`` is the family sum (what the registry reports and what
    # ``test_metrics_registry_snapshot`` reads); the count/min/max/quantile
    # accessors are aggregate over every series in the family.

    def value(self, **labels: object) -> float:
        with self._lock:
            if labels:
                series = self._series.get(_label_key(labels))
                return series.sum if series is not None else 0.0
            return sum(s.sum for s in self._series.values())

    # ``count``/``min``/``max`` stay properties: they were properties before
    # labels existed and callers read them that way, so turning them into
    # methods would break them for no gain. The label-scoped readings are
    # separate ``*_for`` calls.

    @property
    def count(self) -> int:
        """Observations across every series in the family."""
        with self._lock:
            return sum(s.count for s in self._series.values())

    @property
    def min(self) -> float:
        """Smallest observation across every series in the family."""
        with self._lock:
            seen = [s.min for s in self._series.values() if s.count]
            return min(seen) if seen else 0.0

    @property
    def max(self) -> float:
        """Largest observation across every series in the family."""
        with self._lock:
            seen = [s.max for s in self._series.values() if s.count]
            return max(seen) if seen else 0.0

    def count_for(self, **labels: object) -> int:
        """Observations in one label series (0 if that series never occurred)."""
        series = self._one(labels)
        return series.count if series is not None else 0

    def min_for(self, **labels: object) -> float:
        """Smallest observation in one label series (0.0 if it never occurred)."""
        series = self._one(labels)
        return series.min if series is not None and series.count else 0.0

    def max_for(self, **labels: object) -> float:
        """Largest observation in one label series (0.0 if it never occurred)."""
        series = self._one(labels)
        return series.max if series is not None and series.count else 0.0

    def _one(self, labels: dict[str, object]) -> _HistogramSeries | None:
        """The one series addressed by ``labels``; caller must hold the lock."""
        return self._series.get(_label_key(labels))

    def quantile(self, q: float, **labels: object) -> float:
        """Value at quantile ``q`` (``0.95`` for p95), read from the buckets.

        Unlabelled across several series, the buckets are pooled by summing
        their counts — the conservative reading of "this metric's p95".
        """
        with self._lock:
            if labels:
                series = self._series.get(_label_key(labels))
                return series.quantile(q) if series is not None else 0.0
            if not self._series:
                return 0.0
            if len(self._series) == 1:
                return next(iter(self._series.values())).quantile(q)
            pooled = _HistogramSeries(self._bounds)
            for series in self._series.values():
                pooled.count += series.count
                pooled.sum += series.sum
                pooled.min = min(pooled.min, series.min)
                pooled.max = max(pooled.max, series.max)
                for i, c in enumerate(series.counts):
                    pooled.counts[i] += c
            return pooled.quantile(q)

    def _render(self) -> list[str]:
        with self._lock:
            base = self._sanitized()
            lines = [f"# TYPE {base} histogram"]
            for key, series in sorted(self._series.items()):
                for i, bound in enumerate(self._bounds):
                    lines.append(
                        f"{base}_bucket{_bucket_labels(_bound(bound), key)} "
                        f"{series.counts[i]}"
                    )
                # The +Inf bucket is not part of the configured bounds; its
                # count is by definition the total.
                lines.append(
                    f"{base}_bucket{_bucket_labels('+Inf', key)} {series.count}"
                )
                lines.append(f"{base}_sum{_labels(key)} {_value_text(series.sum)}")
                lines.append(f"{base}_count{_labels(key)} {series.count}")
            return lines


def _series_total(series: _Series | _HistogramSeries) -> float:
    """A series' scalar reading: ``sum`` for histograms, ``value`` otherwise."""
    if isinstance(series, _HistogramSeries):
        return series.sum
    return series.value


class MetricsRegistry:
    """Simple metrics collection.

    Thread-safe counter/gauge/histogram registry for tracking operational
    metrics. Each named metric is a family of label-indexed series; the
    aggregate (unlabelled) views fold every series in a family together, so
    callers reading a flat ``{name: value}`` mapping are unaffected by labels.
    """

    def __init__(self) -> None:
        self._counters: dict[str, _Counter] = {}
        self._gauges: dict[str, _Gauge] = {}
        self._histograms: dict[str, _Histogram] = {}

    # -- rich metric API --------------------------------------------------------

    def counter(self, name: str, help_text: str = "") -> _Counter:
        """Return (or create) a named counter family."""
        metric = self._counters.get(name)
        if metric is None:
            metric = _Counter(name, help_text)
            self._counters[name] = metric
        return metric

    def gauge(self, name: str, help_text: str = "") -> _Gauge:
        """Return (or create) a named gauge family."""
        metric = self._gauges.get(name)
        if metric is None:
            metric = _Gauge(name, help_text)
            self._gauges[name] = metric
        return metric

    def histogram(
        self,
        name: str,
        help_text: str = "",
        buckets: tuple[float, ...] = DEFAULT_BUCKETS,
    ) -> _Histogram:
        """Return (or create) a named histogram family."""
        metric = self._histograms.get(name)
        if metric is None:
            metric = _Histogram(name, help_text, buckets)
            self._histograms[name] = metric
        return metric

    def get(self, name: str, **labels: object) -> float:
        """Get the value of any named metric, or of one label series of it.

        Searches counter, gauge and histogram families. Returns 0 for unknown
        metrics.
        """
        for bucket in (self._counters, self._gauges, self._histograms):
            if name in bucket:
                return bucket[name].value(**labels)
        return 0

    def snapshot(self) -> dict[str, float]:
        """Snapshot every metric, one entry per family (labelled series summed)."""
        out: dict[str, float] = {}
        for bucket in (self._counters, self._gauges, self._histograms):
            for name, metric in bucket.items():
                out[name] = metric.value()
        return out

    def series_snapshot(self) -> dict[str, float]:
        """Snapshot every labelled series, keyed ``"<name>|<k=v,k=v>"``.

        ``series_snapshot()`` is the per-label view; :meth:`snapshot` is the
        per-family one. An unlabelled series keys to just ``"<name>"``.
        """
        out: dict[str, float] = {}
        families: list[tuple[str, _Metric[Any]]] = [
            *self._counters.items(),
            *self._gauges.items(),
            *self._histograms.items(),
        ]
        for name, metric in families:
            for key, series in sorted(metric._series.items()):
                total = _series_total(series)  # type: ignore[arg-type]
                out[f"{name}|" + ",".join(f"{k}={v}" for k, v in key)] = total
        return out

    def reset(self) -> None:
        """Reset all metrics."""
        self._counters.clear()
        self._gauges.clear()
        self._histograms.clear()

    def render_prometheus(self) -> str:
        """Render all metrics in Prometheus text exposition format."""
        lines: list[str] = []
        families: list[tuple[str, _Metric[Any]]] = [
            *self._counters.items(),
            *self._gauges.items(),
            *self._histograms.items(),
        ]
        for _, metric in sorted(families):
            lines.extend(metric._render())
        return "\n".join(lines) + ("\n" if lines else "")
