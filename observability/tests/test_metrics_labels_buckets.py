"""Lane 5a — label indexing and Prometheus histogram buckets.

Two properties this file pins, each with a negative control that fails against
the pre-5a implementation (labels discarded via ``del labels``; histograms
carrying only count/min/max/sum):

  1. A metric is a *family of series* keyed by its sorted label tuple, so two
     label sets are two rows and label order is irrelevant.
  2. A histogram carries real cumulative buckets, so p95/p99 are computable
     from bucket counts alone and ``_bucket``/``_sum``/``_count`` render.
"""
from __future__ import annotations

import threading

import pytest
from tradex_observability.metrics import DEFAULT_BUCKETS, MetricsRegistry


def _sample_rows(reg: MetricsRegistry, name: str) -> list[str]:
    """Rendered sample lines for one metric, excluding the ``# TYPE`` header."""
    rows = reg.render_prometheus().splitlines()
    return [row for row in rows if row.startswith(name)]


# ---------------------------------------------------------------------------
# 1. Label indexing
# ---------------------------------------------------------------------------


def test_different_labels_are_distinct_series() -> None:
    reg = MetricsRegistry()
    c = reg.counter("orders.rejection_reason", "per-gate rejections")
    c.inc(1, reason="order_value_exceeded")
    c.inc(1, reason="kill_switch_active")

    # Negative control: pre-5a these were the same float, because ``inc`` ran
    # ``del labels`` and every reason collapsed into one unlabelled row. They
    # could not be told apart, so "two reasons" was unobservable.
    assert c.value(reason="order_value_exceeded") == 1.0
    assert c.value(reason="kill_switch_active") == 1.0
    assert c.value(reason="never_seen") == 0.0
    # The family total is the sum across series.
    assert c.value() == 2.0

    text = reg.render_prometheus()
    assert 'orders_rejection_reason{reason="order_value_exceeded"} 1.0' in text
    assert 'orders_rejection_reason{reason="kill_switch_active"} 1.0' in text


def test_same_labels_aggregate_into_one_series() -> None:
    reg = MetricsRegistry()
    c = reg.counter("orders.rejected")
    c.inc()
    c.inc()
    c.inc(2)
    assert c.value() == 4.0
    # One sample row, not three.
    assert _sample_rows(reg, "orders_rejected") == ["orders_rejected 4.0"]


def test_label_order_does_not_matter() -> None:
    """``{a=1,b=2}`` and ``{b=2,a=1}`` are the same series (sorted tuple)."""
    reg = MetricsRegistry()
    c = reg.counter("bus.messages.published", "published")
    c.inc(1, a="1", b="2")
    c.inc(1, b="2", a="1")

    # Negative control: unsorted keys would key these as two distinct series
    # and both reads would return 0.0 (or the two would render as two rows).
    assert c.value(a="1", b="2") == 2.0
    assert c.value(b="2", a="1") == 2.0
    assert _sample_rows(reg, "bus_messages_published") == [
        'bus_messages_published{a="1",b="2"} 2.0'
    ]


def test_unlabelled_and_labelled_series_coexist() -> None:
    reg = MetricsRegistry()
    c = reg.counter("orders.submitted")
    c.inc()
    c.inc(1, venue="dhan")
    assert c.value() == 2.0
    assert c.value(venue="dhan") == 1.0
    text = reg.render_prometheus()
    assert "orders_submitted 1.0" in text
    assert 'orders_submitted{venue="dhan"} 1.0' in text


def test_gauges_index_by_label_too() -> None:
    reg = MetricsRegistry()
    g = reg.gauge("feed_age_seconds")
    g.set(1.5, symbol="RELIANCE")
    g.set(9.0, symbol="INFY")
    # A gauge set is a replace, so the two symbol series must not share a slot.
    assert g.value(symbol="RELIANCE") == 1.5
    assert g.value(symbol="INFY") == 9.0
    text = reg.render_prometheus()
    assert 'feed_age_seconds{symbol="RELIANCE"} 1.5' in text
    assert 'feed_age_seconds{symbol="INFY"} 9.0' in text


def test_histograms_index_by_label_too() -> None:
    reg = MetricsRegistry()
    h = reg.histogram("orders.submit_latency_seconds")
    h.observe(0.01, venue="dhan")
    h.observe(0.5, venue="upstox")
    assert h.quantile(0.95, venue="dhan") <= 0.01
    assert h.quantile(0.95, venue="upstox") <= 0.5
    text = reg.render_prometheus()
    assert 'orders_submit_latency_seconds_count{venue="dhan"} 1' in text
    assert 'orders_submit_latency_seconds_count{venue="upstox"} 1' in text


def test_label_values_are_escaped() -> None:
    reg = MetricsRegistry()
    reg.counter("weird").inc(1, reason='va"lue')
    assert 'weird{reason="va\\"lue"} 1.0' in reg.render_prometheus()


# ---------------------------------------------------------------------------
# 2. Histogram buckets
# ---------------------------------------------------------------------------


def test_default_buckets_match_the_spec() -> None:
    assert DEFAULT_BUCKETS == (
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


def test_bucket_counts_are_correct_for_a_known_sample() -> None:
    reg = MetricsRegistry()
    h = reg.histogram("orders.submit_latency_seconds")
    # 0.02 -> buckets >= 0.025 ; 0.2 -> >= 0.25 ; 0.6 -> >= 1 ; 7.0 -> >= 10
    # Nothing is <= 0.01, nothing is > 300.
    for v in (0.02, 0.2, 0.6, 7.0):
        h.observe(v)

    text = reg.render_prometheus()
    b = "orders_submit_latency_seconds_bucket"

    def cumulative(le: str) -> int:
        prefix = f'{b}{{le="{le}"}} '
        match = next(row for row in text.splitlines() if row.startswith(prefix))
        return int(match[len(prefix) :])

    # Negative control: pre-5a there were no `_bucket` lines at all, so this
    # lookup would raise StopIteration — p95/p99 were simply uncomputable.
    assert cumulative("0.01") == 0
    assert cumulative("0.025") == 1
    assert cumulative("0.1") == 1
    assert cumulative("0.25") == 2
    assert cumulative("0.5") == 2
    assert cumulative("1") == 3
    assert cumulative("5") == 3
    assert cumulative("10") == 4
    assert cumulative("300") == 4
    # +Inf is the total.
    assert cumulative("+Inf") == 4
    assert h.count == 4
    assert h.value() == pytest.approx(7.82)
    assert h.min == 0.02
    assert h.max == 7.0


def test_p95_and_p99_on_a_known_sample() -> None:
    """p95/p99 come out of the buckets, which is what lane 5b's rules need."""
    reg = MetricsRegistry()
    h = reg.histogram("orders.submit_latency_seconds")
    # 20 samples, one per 50 ms from 0.05 to 1.0. The p95 observation is the
    # 19th of 20 (rank = ceil(0.95*20) = 19) -> 0.95 s; p99 rank = 20 -> 1.0 s.
    for i in range(1, 21):
        h.observe(i * 0.05)

    assert h.count == 20
    assert h.quantile(0.50) == pytest.approx(0.5)
    assert h.quantile(0.95) == pytest.approx(0.95)
    assert h.quantile(0.99) == pytest.approx(1.0)
    # Negative control: pre-5a ``quantile`` did not exist, so p95/p99 rows in
    # the catalog were not computable at all.
    assert h.quantile(0.0) == 0.05
    assert h.quantile(1.0) == 1.0


def test_quantile_of_an_empty_histogram_is_zero() -> None:
    reg = MetricsRegistry()
    assert reg.histogram("never.observed").quantile(0.95) == 0.0


def test_quantile_above_the_last_bucket_falls_back_to_max() -> None:
    reg = MetricsRegistry()
    h = reg.histogram("sparse", buckets=(1.0, 2.0))
    h.observe(0.5)
    h.observe(100.0)
    assert h.quantile(0.99) == 100.0


def test_custom_buckets() -> None:
    reg = MetricsRegistry()
    h = reg.histogram("orders.submit_latency_seconds", "SLO", buckets=(0.25, 0.75))
    h.observe(0.1)
    h.observe(0.5)
    h.observe(0.9)
    text = reg.render_prometheus()
    assert 'orders_submit_latency_seconds_bucket{le="0.25"} 1' in text
    assert 'orders_submit_latency_seconds_bucket{le="0.75"} 2' in text
    assert 'orders_submit_latency_seconds_bucket{le="+Inf"} 3' in text


# ---------------------------------------------------------------------------
# 3. render_prometheus
# ---------------------------------------------------------------------------


def test_render_emits_bucket_sum_and_count() -> None:
    reg = MetricsRegistry()
    h = reg.histogram("latency_seconds", "Latency")
    h.observe(0.1)
    h.observe(0.3)
    text = reg.render_prometheus()

    # Negative control: pre-5a the histogram rendered as
    # ``# TYPE latency_seconds summary`` with hand-rolled ``_min``/``_max``
    # lines, which Prometheus cannot parse as a summary at all.
    assert "# TYPE latency_seconds histogram" in text
    assert "latency_seconds_count 2" in text
    assert "latency_seconds_sum 0.4" in text
    assert 'latency_seconds_bucket{le="0.1"} 1' in text
    assert 'latency_seconds_bucket{le="0.25"} 1' in text
    assert 'latency_seconds_bucket{le="0.5"} 2' in text
    assert 'latency_seconds_bucket{le="+Inf"} 2' in text
    assert "latency_seconds_min" not in text


def test_render_emits_per_label_rows() -> None:
    reg = MetricsRegistry()
    reg.counter("orders.rejection_reason").inc(1, reason="drawdown_exceeded")
    reg.counter("orders.rejection_reason").inc(3, reason="kill_switch_active")
    text = reg.render_prometheus()
    assert 'orders_rejection_reason{reason="drawdown_exceeded"} 1.0' in text
    assert 'orders_rejection_reason{reason="kill_switch_active"} 3.0' in text


def test_render_buckets_carry_labels() -> None:
    reg = MetricsRegistry()
    h = reg.histogram("latency_seconds")
    h.observe(0.01, venue="dhan")
    text = reg.render_prometheus()
    assert 'latency_seconds_bucket{le="0.01",venue="dhan"} 1' in text
    assert 'latency_seconds_bucket{le="+Inf",venue="dhan"} 1' in text
    assert 'latency_seconds_count{venue="dhan"} 1' in text


def test_render_sanitizes_names() -> None:
    reg = MetricsRegistry()
    reg.counter("my.metric-name").inc(1)
    text = reg.render_prometheus()
    assert "my_metric_name 1.0" in text
    assert "my.metric-name" not in text


def test_render_empty_registry() -> None:
    assert MetricsRegistry().render_prometheus() == ""


# ---------------------------------------------------------------------------
# 4. Registry-level views
# ---------------------------------------------------------------------------


def test_snapshot_folds_series_into_the_family_total() -> None:
    """Existing flat-name consumers keep working: one entry per metric name."""
    reg = MetricsRegistry()
    c = reg.counter("orders.rejected")
    c.inc(1, reason="a")
    c.inc(1, reason="b")
    assert reg.snapshot()["orders.rejected"] == 2.0
    assert reg.get("orders.rejected") == 2.0
    assert reg.get("orders.rejected", reason="a") == 1.0
    assert reg.get("nope") == 0


def test_series_snapshot_exposes_per_label_rows() -> None:
    reg = MetricsRegistry()
    c = reg.counter("orders.rejected")
    c.inc(1, reason="a")
    c.inc(1, reason="b")
    assert reg.series_snapshot() == {
        "orders.rejected|reason=a": 1.0,
        "orders.rejected|reason=b": 1.0,
    }


def test_reset_clears_series_too() -> None:
    reg = MetricsRegistry()
    c = reg.counter("x")
    c.inc(1, reason="a")
    reg.reset()
    assert reg.snapshot() == {}
    assert reg.counter("x").value(reason="a") == 0.0


def test_count_min_max_stay_properties_and_add_label_scoped_reads() -> None:
    """``count``/``min``/``max`` were properties before labels existed.

    ``test_execution_engine.py::test_submit_latency_histogram_observed`` reads
    ``hist.count`` as a property, so turning it into a method to accept labels
    would break that caller. The label-scoped reading is a separate call.
    """
    reg = MetricsRegistry()
    h = reg.histogram("orders.submit_latency_seconds")
    h.observe(0.1, venue="dhan")
    h.observe(5.0, venue="upstox")

    # Properties: the family aggregate, exactly as they behaved before.
    assert h.count == 2
    assert h.min == 0.1
    assert h.max == 5.0

    # Label-scoped reads are the new, separate surface.
    assert h.count_for(venue="dhan") == 1
    assert h.max_for(venue="dhan") == 0.1
    assert h.min_for(venue="upstox") == 5.0
    assert h.count_for(venue="never") == 0
    assert h.min_for(venue="never") == 0.0


# ---------------------------------------------------------------------------
# 5. Thread safety
# ---------------------------------------------------------------------------


def test_concurrent_labelled_increments_do_not_lose_counts() -> None:
    reg = MetricsRegistry()
    c = reg.counter("bus.messages.published")

    def worker() -> None:
        for _ in range(500):
            c.inc(1, venue="dhan")

    threads = [threading.Thread(target=worker) for _ in range(8)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()

    # H1's lock now guards both the series index and the value, so 8x500
    # increments into one labelled series all land.
    assert c.value(venue="dhan") == 4000.0


def test_concurrent_labelled_and_unlabelled_series_stay_separate() -> None:
    reg = MetricsRegistry()
    h = reg.histogram("latency_seconds")

    def worker(venue: str) -> None:
        for i in range(200):
            h.observe(0.01 * (i % 5 + 1), venue=venue)

    threads = [
        threading.Thread(target=worker, args=(v,)) for v in ("a", "b", "c", "d")
    ]
    for t in threads:
        t.start()
    for t in threads:
        t.join()

    assert h.count_for(venue="a") == 200
    assert h.count_for(venue="d") == 200
    assert h.count == 800
