"""StrategySpec declaration + startup validation (Phase 3)."""

from __future__ import annotations

from types import SimpleNamespace

import pytest
from tradex_analytics.registry import REGISTRY
from tradex_domain import Timeframe
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
    problems = validate_strategy_spec(_spec(), REGISTRY)
    assert problems == []


def test_validate_rejects_unknown_indicator() -> None:
    problems = validate_strategy_spec(
        _spec(required_indicators=("not_a_real_indicator",)), REGISTRY
    )
    assert any("not_a_real_indicator" in p for p in problems)


def test_validate_rejects_unknown_feature() -> None:
    problems = validate_strategy_spec(
        _spec(required_features=("not_a_feature",)), REGISTRY
    )
    assert any("not_a_feature" in p for p in problems)


def test_validate_rejects_unsupported_timeframe() -> None:
    problems = validate_strategy_spec(_spec(timeframes=("2m",)), REGISTRY)
    assert any("2m" in p for p in problems)


def test_validate_accepts_empty_timeframes() -> None:
    """A quote-driven strategy may declare no timeframes at all."""
    problems = validate_strategy_spec(
        _spec(timeframes=(), required_indicators=()), REGISTRY
    )
    assert problems == []


def test_validate_rejects_insufficient_warmup() -> None:
    problems = validate_strategy_spec(_spec(warmup_bars=0), REGISTRY)
    assert any("warmup" in p for p in problems)


def test_validate_accepts_zero_warmup_without_indicators() -> None:
    """No declared indicator means nothing needs warming up."""
    problems = validate_strategy_spec(
        _spec(required_indicators=(), warmup_bars=0), REGISTRY
    )
    assert problems == []


def test_validate_rejects_unknown_subscription() -> None:
    problems = validate_strategy_spec(_spec(subscriptions=("Telemetry",)), REGISTRY)
    assert any("Telemetry" in p for p in problems)


def test_validate_accepts_known_and_empty_subscriptions() -> None:
    assert validate_strategy_spec(_spec(subscriptions=()), REGISTRY) == []
    assert validate_strategy_spec(_spec(subscriptions=("Candle", "Quote")), REGISTRY) == []


def test_boot_rejects_a_bad_declared_spec(monkeypatch) -> None:
    """boot() must refuse to start when a discovered strategy's spec is bad.

    This reaches the real validation seam in ``_boot_tail``: a strategy whose
    spec names an unknown indicator is a startup error, not a first-candle
    runtime failure.
    """
    import tradex_runtime.startup as startup
    from tradex_config.schema import AppConfig
    from trading.tests.risk_support import permissive_risk

    bad = SimpleNamespace(
        spec=StrategySpec(id="bad_spec", required_indicators=("not_a_real_indicator",)),
    )
    monkeypatch.setattr(startup, "all_strategies", (bad,))
    with pytest.raises(ValueError, match="not_a_real_indicator"):
        startup.boot(AppConfig(risk=permissive_risk(), mode="paper"))
