"""Per-symbol event routing for the strategy engine.

Strategies declare the instruments they trade; the engine routes each
market event only to the strategies subscribed to that instrument.
Strategies that declare nothing (portfolio/multi-symbol strategies that
trade every instrument) receive a wildcard subscription — every event.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any

from tradex_domain.instruments import InstrumentId


@dataclass(frozen=True, slots=True)
class StrategySubscription:
    """One strategy's subscription to one event type.

    ``instrument_id=None`` is a wildcard: the strategy receives every
    instrument's events of that type.
    """

    event_type: type
    instrument_id: InstrumentId | None = None


class SubscriptionRouter:
    """Indexes strategy subscriptions by (event_type, instrument_id).

    A wildcard set per event type holds strategies subscribed to all
    instruments. ``delivers`` is the hot-path check the engine's wrapper
    handlers call before evaluating a strategy.
    """

    def __init__(self) -> None:
        self._index: dict[tuple[type, InstrumentId], set[str]] = {}
        self._wildcard: dict[type, set[str]] = {}

    def subscribe(self, sub: StrategySubscription, strategy_id: str) -> None:
        if sub.instrument_id is None:
            self._wildcard.setdefault(sub.event_type, set()).add(strategy_id)
        else:
            self._index.setdefault((sub.event_type, sub.instrument_id), set()).add(
                strategy_id
            )

    def unsubscribe(self, strategy_id: str) -> None:
        for ids in self._index.values():
            ids.discard(strategy_id)
        for ids in self._wildcard.values():
            ids.discard(strategy_id)

    def delivers(
        self, strategy_id: str, event_type: type, instrument_id: InstrumentId
    ) -> bool:
        if strategy_id in self._wildcard.get(event_type, ()):
            return True
        return strategy_id in self._index.get((event_type, instrument_id), ())


def event_instrument_id(event: Any) -> InstrumentId | None:
    """Best-effort instrument id of a market event; None when absent.

    Test fakes and some legacy events carry no instrument. Routing
    cannot key on a missing instrument, so the engine delivers those to
    every subscriber (conservative).
    """
    inst = getattr(event, "instrument", None)
    if inst is None:
        return None
    return getattr(inst, "instrument_id", None)
