"""Unified signal construction and strategy registry."""

from __future__ import annotations

from typing import Any

from tradex_domain import OrderSide, Signal


def build_signal(
    instrument: Any,
    direction: OrderSide,
    reason: str,
    timestamp: Any = None,
    *,
    strength: float = 1.0,
    stop: float | None = None,
    target: float | None = None,
    metadata: dict[str, Any] | None = None,
) -> Signal:
    meta = dict(metadata) if metadata else {}
    if stop is not None and target is not None:
        meta["stop_loss_price"] = stop
        meta["target_price"] = target
    return Signal(
        instrument=instrument,
        direction=direction,
        strength=strength,
        reason=reason,
        metadata=meta,
        timestamp=timestamp,
    )


class StrategyRegistry:
    def __init__(self) -> None:
        self._strategies: dict[str, Any] = {}

    def register(self, name: str, instance: Any) -> None:
        self._strategies[name] = instance

    def get(self, name: str) -> Any | None:
        return self._strategies.get(name)

    def all(self) -> dict[str, Any]:
        return dict(self._strategies)


_registry = StrategyRegistry()


def register_strategy(name: str, instance: Any) -> None:
    _registry.register(name, instance)


def get_strategy(name: str) -> Any | None:
    return _registry.get(name)
