"""Strategy definition: a factory + spec, separated from the runtime instance."""

from __future__ import annotations

from collections.abc import Callable
from typing import Any

from tradex_domain.instruments import Instrument
from tradex_domain.strategy import StrategySpec


class StrategyDefinition:
    """A strategy factory that can produce one instance per instrument.

    The definition is the stable, stateless half (what the strategy IS);
    the instance is the stateful half bound to one instrument (what it
    TRADES). Separating them lets the composition root create one instance
    per symbol without the strategy class knowing it is multi-symbol.
    """

    def __init__(
        self,
        spec: StrategySpec,
        create: Callable[..., Any],
    ) -> None:
        self.spec = spec
        self.create = create

    def instantiate(self, instrument: Instrument, **config: Any) -> Any:
        return self.create(instrument, **config)


def instantiate_definitions(
    definitions: list[StrategyDefinition],
    universe: list[Instrument],
    **config: Any,
) -> list:
    """Expand definitions × universe into per-binding instances."""
    return [
        definition.instantiate(instrument, **config)
        for definition in definitions
        for instrument in universe
    ]
