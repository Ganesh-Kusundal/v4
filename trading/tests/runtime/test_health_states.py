"""Tests for new ComponentState health states and severity ordering."""

from __future__ import annotations

from tradex_trading.runtime.health import (
    _SEVERITY,
    AggregateHealthCheck,
    ComponentHealth,
    ComponentState,
)


class _StaticCheck:
    """Minimal HealthCheck stand-in returning a fixed state."""

    def __init__(self, component_id: str, state: ComponentState) -> None:
        self._id = component_id
        self._state = state

    def check(self) -> ComponentHealth:
        return ComponentHealth(component_id=self._id, state=self._state)


class TestNewHealthStates:
    def test_initializing_state_exists(self) -> None:
        assert ComponentState.INITIALIZING == "INITIALIZING"
        assert "INITIALIZING" in ComponentState.__members__

    def test_starting_state_exists(self) -> None:
        assert ComponentState.STARTING == "STARTING"
        assert "STARTING" in ComponentState.__members__

    def test_stopping_state_exists(self) -> None:
        assert ComponentState.STOPPING == "STOPPING"
        assert "STOPPING" in ComponentState.__members__

    def test_severity_ordering(self) -> None:
        assert _SEVERITY["ERROR"] > _SEVERITY["STOPPING"]
        assert _SEVERITY["STOPPING"] > _SEVERITY["DEGRADED"]
        assert _SEVERITY["DEGRADED"] > _SEVERITY["STARTING"]
        assert _SEVERITY["STARTING"] > _SEVERITY["RUNNING"]

    def test_aggregate_with_new_states(self) -> None:
        checks = [
            _StaticCheck("bus", ComponentState.RUNNING),
            _StaticCheck("cache", ComponentState.STARTING),
            _StaticCheck("clock", ComponentState.INITIALIZING),
        ]
        agg = AggregateHealthCheck(checks)
        result = agg.check()
        # STARTING and INITIALIZING both have severity 1; worst should be one of them
        assert result.state in (ComponentState.STARTING, ComponentState.INITIALIZING)
        assert result.component_id == "aggregate"
