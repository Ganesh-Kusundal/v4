"""StrategySpec declaration + startup validation (Phase 3)."""

from __future__ import annotations

import pytest
from tradex_analytics.registry import REGISTRY
from tradex_domain import Equity, Timeframe  # noqa: F401
from tradex_domain.strategy import StrategySpec
from tradex_strategy.core.spec_validator import validate_strategy_spec


def _spec(**overrides) -> StrategySpec:
    base = dict(
        id="test_strategy",
        version="1.0.0",
        scope="INSTRUMENT",
        timeframes=(Timeframe.M1,),
        warmup_bars=20,
        required_indicators=("sma",),
        required_features=(),
    )
    base.update(overrides)
    return StrategySpec(**base)


def test_spec_is_frozen() -> None:
    spec = _spec()
    with pytest.raises(Exception):
        spec.warmup_bars = 99  # type: ignore[misc]


def test_validate_accepts_known_indicator() -> None:
    problems = validate_strategy_spec(_spec(), REGISTRY, universe=[])
    assert problems == []


def test_validate_rejects_unknown_indicator() -> None:
    problems = validate_strategy_spec(
        _spec(required_indicators=("not_a_real_indicator",)), REGISTRY, universe=[]
    )
    assert any("not_a_real_indicator" in p for p in problems)


def test_validate_rejects_unknown_feature() -> None:
    problems = validate_strategy_spec(
        _spec(required_features=("not_a_feature",)), REGISTRY, universe=[]
    )
    assert any("not_a_feature" in p for p in problems)
