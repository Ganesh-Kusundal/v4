"""Tests for the /metrics Prometheus exposition endpoint (G14)."""

from __future__ import annotations

import pytest

fastapi = pytest.importorskip("fastapi")
from fastapi.testclient import TestClient  # noqa: E402

from tradex_trading.interface.fastapi_app import create_app  # noqa: E402
from tradex_trading.runtime.metrics import MetricsRegistry  # noqa: E402


# ---------------------------------------------------------------------------
# render_prometheus unit tests
# ---------------------------------------------------------------------------


def test_render_counter_and_histogram():
    """A registry with a counter and histogram renders valid Prometheus text."""
    reg = MetricsRegistry()
    c = reg.counter("orders_total", "Total orders")
    c.inc(5)
    h = reg.histogram("latency_seconds", "Latency")
    h.observe(0.1)
    h.observe(0.3)

    text = reg.render_prometheus()
    assert "# TYPE orders_total counter" in text
    assert "orders_total 5.0" in text
    assert "# TYPE latency_seconds summary" in text
    assert "latency_seconds_count 2" in text
    assert "latency_seconds_sum 0.4" in text
    assert "latency_seconds_min 0.1" in text
    assert "latency_seconds_max 0.3" in text


def test_render_gauge():
    """Gauges render with TYPE gauge."""
    reg = MetricsRegistry()
    g = reg.gauge("active_connections")
    g.set(42)
    text = reg.render_prometheus()
    assert "# TYPE active_connections gauge" in text
    assert "active_connections 42" in text


def test_render_sanitizes_names():
    """Metric names with dashes/dots are sanitized to underscores."""
    reg = MetricsRegistry()
    reg.counter("my.metric-name").inc(1)
    text = reg.render_prometheus()
    assert "my_metric_name 1.0" in text
    assert "my.metric-name" not in text


def test_render_empty_registry():
    """An empty registry returns an empty string."""
    reg = MetricsRegistry()
    assert reg.render_prometheus() == ""


# ---------------------------------------------------------------------------
# /metrics endpoint integration tests
# ---------------------------------------------------------------------------


def test_metrics_endpoint_returns_200_text_plain():
    """GET /metrics returns 200 with Prometheus content type."""
    app = create_app()
    client = TestClient(app)
    r = client.get("/metrics")
    assert r.status_code == 200
    assert "text/plain" in r.headers["content-type"]


def test_metrics_endpoint_reflects_registered_metric():
    """Metrics registered on app.state.metrics appear in GET /metrics."""
    app = create_app()
    reg = app.state.metrics
    reg.counter("test_requests_total").inc(7)
    client = TestClient(app)
    r = client.get("/metrics")
    assert r.status_code == 200
    assert "test_requests_total 7.0" in r.text


def test_metrics_endpoint_empty_registry_returns_200():
    """An empty registry still returns 200 (no body or headers-only is fine)."""
    app = create_app()
    client = TestClient(app)
    r = client.get("/metrics")
    assert r.status_code == 200
