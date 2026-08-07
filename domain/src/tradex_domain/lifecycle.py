"""Component lifecycle management (spec §03)."""

from __future__ import annotations

import logging
from abc import ABC, abstractmethod
from enum import StrEnum
from typing import Any

log = logging.getLogger(__name__)


class LifecycleState(StrEnum):
    """Component lifecycle states per spec."""
    UNINITIALIZED = "UNINITIALIZED"
    INITIALIZED = "INITIALIZED"
    RUNNING = "RUNNING"
    STOPPED = "STOPPED"
    ERROR = "ERROR"


_VALID_TRANSITIONS: dict[LifecycleState, frozenset[LifecycleState]] = {
    LifecycleState.UNINITIALIZED: frozenset({LifecycleState.INITIALIZED, LifecycleState.ERROR}),
    LifecycleState.INITIALIZED: frozenset(
        {LifecycleState.RUNNING, LifecycleState.STOPPED, LifecycleState.ERROR}
    ),
    LifecycleState.RUNNING: frozenset({LifecycleState.STOPPED, LifecycleState.ERROR}),
    LifecycleState.STOPPED: frozenset({LifecycleState.INITIALIZED}),  # restart
    LifecycleState.ERROR: frozenset({LifecycleState.INITIALIZED}),   # recovery
}


class Component(ABC):
    """Abstract base for lifecycle-managed components."""

    def __init__(self) -> None:
        self._state = LifecycleState.UNINITIALIZED

    @property
    def state(self) -> LifecycleState:
        return self._state

    def transition_to(self, new_state: LifecycleState) -> None:
        allowed = _VALID_TRANSITIONS.get(self._state, frozenset())
        if new_state not in allowed:
            raise RuntimeError(f"Invalid lifecycle transition: {self._state} -> {new_state}")
        log.info("Component %s: %s -> %s", type(self).__name__, self._state.value, new_state.value)
        self._state = new_state

    @abstractmethod
    def start(self) -> None: ...

    @abstractmethod
    def stop(self) -> None: ...

    def health(self) -> dict[str, Any]:
        return {"component": type(self).__name__, "state": self._state.value}


class LifecycleManager:
    """Manages ordered startup/shutdown of components."""

    def __init__(self) -> None:
        self._components: list[Component] = []

    def register(self, component: Component) -> None:
        self._components.append(component)

    def start_all(self) -> None:
        for comp in self._components:
            if comp.state == LifecycleState.UNINITIALIZED:
                comp.transition_to(LifecycleState.INITIALIZED)
            if comp.state == LifecycleState.INITIALIZED:
                comp.start()
                comp.transition_to(LifecycleState.RUNNING)

    def stop_all(self) -> None:
        for comp in reversed(self._components):
            if comp.state == LifecycleState.RUNNING:
                comp.stop()
                comp.transition_to(LifecycleState.STOPPED)

    @property
    def components(self) -> list[Component]:
        return list(self._components)

    def health_report(self) -> list[dict[str, Any]]:
        return [c.health() for c in self._components]


__all__ = ["Component", "LifecycleManager", "LifecycleState"]
