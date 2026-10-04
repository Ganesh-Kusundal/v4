"""Validate a strategy's declared requirements before it activates."""

from __future__ import annotations

from typing import Any

from tradex_analytics.indicators import get_indicator_spec
from tradex_domain.strategy import StrategySpec


def validate_strategy_spec(
    spec: StrategySpec,
    registry: Any | None = None,
    universe: list | None = None,
) -> list[str]:
    """Return a list of problems (empty = the spec can be satisfied).

    Checks each required indicator against the analytics registry and each
    required feature against the known feature set. A strategy declaring a
    requirement the platform cannot meet is a startup error, not a
    first-candle runtime failure.

    *registry* defaults to the analytics module-level ``REGISTRY`` so callers
    that may not import ``tradex_analytics`` (e.g. the runtime composition
    root) can validate a spec without holding a registry themselves.
    """
    problems: list[str] = []
    for name in spec.required_indicators:
        if get_indicator_spec(name.lower(), registry) is None:
            problems.append(
                f"strategy {spec.id!r} requires indicator {name!r} "
                "which is not implemented"
            )
    # Features: no FeatureRegistry yet — treat any non-empty requirement
    # as unmet until one exists. This is the seam Phase 2's FeatureStore
    # plugs into; for now it fails closed on unknown features.
    for name in spec.required_features:
        problems.append(
            f"strategy {spec.id!r} requires feature {name!r} "
            "which is not implemented"
        )
    return problems
