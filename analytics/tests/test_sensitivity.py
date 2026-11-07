"""Tests for sensitivity analysis."""

from tradex_analytics.sensitivity import sensitivity_scan


def test_sensitivity_scan():
    def evaluate(params):
        # metric peaks at x=5
        x = params["x"]
        return -(x - 5) ** 2 + 10

    result = sensitivity_scan("x", [1, 3, 5, 7, 9], evaluate)
    assert result.best_value == 5
    assert result.best_metric == 10
    assert len(result.metric_values) == 5


def test_sensitivity_with_base_params():
    def evaluate(params):
        return params["x"] + params["y"]

    result = sensitivity_scan("x", [1, 2, 3], evaluate, base_params={"y": 10})
    assert result.best_value == 3
    assert result.best_metric == 13
