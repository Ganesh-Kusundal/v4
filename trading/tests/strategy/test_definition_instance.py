"""Definition/Instance separation (Phase 4): one definition, many isolated instances."""

from __future__ import annotations

from tradex_domain import Equity, Timeframe
from tradex_domain.strategy import StrategySpec
from tradex_strategy.core.definitions import StrategyDefinition, instantiate_definitions
from tradex_strategy.extensions.strategies.sma_cross import SmaCrossStrategy


def _definition() -> StrategyDefinition:
    return StrategyDefinition(
        spec=StrategySpec(
            id="sma_cross",
            version="1.0.0",
            scope="INSTRUMENT",
            timeframes=(Timeframe.M1,),
            warmup_bars=20,
            required_indicators=("sma",),
        ),
        create=lambda instrument, **config: SmaCrossStrategy(
            strategy_id="sma_cross", instrument=instrument, **config
        ),
    )


def test_one_definition_many_instances() -> None:
    universe = [Equity.of("NSE", s) for s in ("RELIANCE", "TCS", "INFY")]
    instances = instantiate_definitions([_definition()], universe)
    assert len(instances) == 3
    assert [i.instrument.symbol for i in instances] == ["RELIANCE", "TCS", "INFY"]


def test_instances_have_isolated_state() -> None:
    universe = [Equity.of("NSE", s) for s in ("RELIANCE", "TCS")]
    instances = instantiate_definitions([_definition()], universe)
    reliance, tcs = instances
    # Mutate RELIANCE's state; TCS must be unaffected.
    reliance._closes.append(999.0)
    assert 999.0 not in tcs._closes


def test_definition_without_instrument_uses_default() -> None:
    """A definition whose factory ignores the instrument still yields an instance."""
    definition = StrategyDefinition(
        spec=StrategySpec(id="x", warmup_bars=0),
        create=lambda instrument, **config: SmaCrossStrategy("x", instrument),
    )
    (instance,) = instantiate_definitions([definition], [Equity.of("NSE", "RELIANCE")])
    assert instance.instrument.symbol == "RELIANCE"
