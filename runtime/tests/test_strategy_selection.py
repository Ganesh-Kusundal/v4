"""Live strategy selection is explicit; examples are never implied."""

from __future__ import annotations

from dataclasses import replace
from types import SimpleNamespace

import pytest
from tradex_config.schema import AppConfig
from tradex_runtime.startup import _select_strategies


class _Strategy:
    def __init__(self, strategy_id: str) -> None:
        self.strategy_id = strategy_id


class _Definition:
    def __init__(self, definition_id: str) -> None:
        self.spec = SimpleNamespace(id=definition_id)


def _cfg(mode: str, *, ids: tuple[str, ...] = (), auto: bool = False) -> AppConfig:
    cfg = AppConfig(mode=mode)
    return replace(
        cfg,
        execution=replace(
            cfg.execution, strategy_ids=ids, auto_register_examples=auto
        ),
    )


DEPLOYED = _Strategy("deployed")
EXAMPLE = _Strategy("sma_cross_example")
DEFINITION = _Definition("sma_cross")


def test_live_refuses_auto_registered_examples() -> None:
    with pytest.raises(ValueError, match="auto_register_examples"):
        _select_strategies(_cfg("live", auto=True), (EXAMPLE,), ())


def test_live_requires_at_least_one_named_strategy() -> None:
    with pytest.raises(ValueError, match="TRADEX_STRATEGY_IDS"):
        _select_strategies(_cfg("live"), (EXAMPLE,), ())


def test_live_refuses_an_unknown_strategy_id() -> None:
    with pytest.raises(ValueError, match="not_a_strategy"):
        _select_strategies(_cfg("live", ids=("not_a_strategy",)), (DEPLOYED,), ())


def test_live_returns_only_the_named_instance_and_definition() -> None:
    strategies, definitions = _select_strategies(
        _cfg("live", ids=("deployed", "sma_cross")),
        (DEPLOYED, EXAMPLE),
        (DEFINITION,),
    )

    assert strategies == (DEPLOYED,)
    assert definitions == (DEFINITION,)


def test_paper_keeps_the_auto_register_switch() -> None:
    strategies, definitions = _select_strategies(
        _cfg("paper", auto=True), (EXAMPLE,), (DEFINITION,)
    )
    assert strategies == (EXAMPLE,)
    assert definitions == (DEFINITION,)

    empty, no_defs = _select_strategies(_cfg("paper"), (EXAMPLE,), (DEFINITION,))
    assert empty == ()
    assert no_defs == ()
