"""Parameter sensitivity analysis — metric vs. parameter heatmaps."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Callable


@dataclass
class SensitivityResult:
    param_name: str
    param_values: list[Any]
    metric_values: list[float]
    best_value: Any
    best_metric: float


def sensitivity_scan(
    param_name: str,
    param_values: list[Any],
    evaluate: Callable[[dict[str, Any]], float],
    base_params: dict[str, Any] | None = None,
) -> SensitivityResult:
    """Scan a single parameter and record metric for each value."""
    base = base_params or {}
    metrics = []
    for v in param_values:
        params = {**base, param_name: v}
        m = evaluate(params)
        metrics.append(m)
    best_idx = max(range(len(metrics)), key=lambda i: metrics[i])
    return SensitivityResult(
        param_name=param_name,
        param_values=param_values,
        metric_values=metrics,
        best_value=param_values[best_idx],
        best_metric=metrics[best_idx],
    )
