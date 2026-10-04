"""Validate a strategy's declared requirements before it activates."""

from __future__ import annotations

from typing import Any

from tradex_analytics.indicators import get_indicator_spec
from tradex_domain.enums import Timeframe
from tradex_domain.execution import Fill
from tradex_domain.market import Candle, Depth, Quote
from tradex_domain.strategy import StrategySpec

#: Every timeframe the platform can serve. A strategy's declared timeframes
#: must be a subset of this set; an empty declaration is valid (the strategy
#: may be quote-driven and never request a bar series).
_SUPPORTED_TIMEFRAMES = frozenset(tf.value for tf in Timeframe)

#: Event types a strategy may subscribe to, by class name.
_KNOWN_SUBSCRIPTIONS = frozenset(cls.__name__ for cls in (Candle, Quote, Depth, Fill))


def validate_strategy_spec(
    spec: StrategySpec,
    registry: Any | None = None,
) -> list[str]:
    """Return a list of problems (empty = the spec can be satisfied).

    Checks each required indicator against the analytics registry, each
    required feature against the known feature set, every declared timeframe
    against the supported set, warmup sufficiency for the declared indicators,
    and every subscription against the known event types. A strategy declaring
    a requirement the platform cannot meet is a startup error, not a
    first-candle runtime failure.

    *registry* defaults to the analytics module-level ``REGISTRY`` so callers
    that may not import ``tradex_analytics`` (e.g. the runtime composition
    root) can validate a spec without holding a registry themselves.

    Warmup limitation: ``IndicatorSpec`` has no ``warmup_bars`` field yet, so
    the check is pragmatic — a strategy that declares indicators must declare a
    positive warmup. It cannot yet prove that ``warmup_bars`` covers the
    longest declared indicator; that needs ``IndicatorSpec.warmup_bars`` (a
    later phase).
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

    for timeframe in spec.timeframes:
        value = getattr(timeframe, "value", timeframe)
        if value not in _SUPPORTED_TIMEFRAMES:
            problems.append(
                f"strategy {spec.id!r} declares unsupported timeframe {value!r}"
            )

    if spec.required_indicators and spec.warmup_bars <= 0:
        problems.append(
            f"strategy {spec.id!r} declares indicators "
            f"{spec.required_indicators!r} but warmup_bars is not positive "
            "(insufficient warmup)"
        )

    for name in spec.subscriptions:
        if name not in _KNOWN_SUBSCRIPTIONS:
            problems.append(
                f"strategy {spec.id!r} declares unknown subscription {name!r} "
                f"(known: {sorted(_KNOWN_SUBSCRIPTIONS)})"
            )

    return problems
