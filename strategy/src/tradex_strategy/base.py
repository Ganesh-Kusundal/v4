"""Strategy base class — template method pattern for zero-discrepancy.

All strategies inherit from ``BaseStrategy`` and implement ``on_bar``.
The lifecycle (start/stop/on_event) is handled by the base class, ensuring
identical behavior across backtest/live/paper/replay.
"""

from __future__ import annotations

from abc import ABC, abstractmethod
from dataclasses import dataclass, field
from typing import Any


@dataclass
class StrategyContext:
    """Context passed to strategy on each bar."""

    portfolio: Any
    clock: Any
    risk_manager: Any = None
    params: dict[str, Any] = field(default_factory=dict)


class BaseStrategy(ABC):
    """Template-method strategy base."""

    def __init__(self, params: dict[str, Any] | None = None) -> None:
        self.params = params or {}
        self._started = False
        self._stopped = False

    @property
    def name(self) -> str:
        return self.__class__.__name__

    def start(self, ctx: StrategyContext) -> None:
        self._ctx = ctx
        self._started = True
        self.on_start(ctx)

    def stop(self) -> None:
        self._stopped = True
        self.on_stop()

    @abstractmethod
    def on_bar(self, bar: dict[str, Any], ctx: StrategyContext) -> list[dict[str, Any]]:
        """Return list of order dicts."""
        ...

    def on_start(self, ctx: StrategyContext) -> None:
        pass

    def on_stop(self) -> None:
        pass

    def on_event(self, event: Any, ctx: StrategyContext) -> None:
        pass

    @property
    def is_started(self) -> bool:
        return self._started

    @property
    def is_stopped(self) -> bool:
        return self._stopped
