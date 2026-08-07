"""Tests for Component lifecycle and LifecycleManager."""

from __future__ import annotations

import pytest

from tradex_domain.lifecycle import Component, LifecycleManager, LifecycleState

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

class _StubComponent(Component):
    """Concrete Component for testing."""

    def __init__(self, name: str = "Stub") -> None:
        super().__init__()
        self.name = name
        self.started = False
        self.stopped = False

    def start(self) -> None:
        self.started = True

    def stop(self) -> None:
        self.stopped = True

    def health(self):
        base = super().health()
        base["name"] = self.name
        return base


class _FailingComponent(Component):
    """Component whose start() raises."""

    def start(self) -> None:
        raise RuntimeError("boom")

    def stop(self) -> None:
        pass


# ---------------------------------------------------------------------------
# Valid transitions
# ---------------------------------------------------------------------------

class TestValidTransitions:
    def test_uninitialized_to_initialized(self):
        comp = _StubComponent()
        comp.transition_to(LifecycleState.INITIALIZED)
        assert comp.state == LifecycleState.INITIALIZED

    def test_uninitialized_to_error(self):
        comp = _StubComponent()
        comp.transition_to(LifecycleState.ERROR)
        assert comp.state == LifecycleState.ERROR

    def test_initialized_to_running(self):
        comp = _StubComponent()
        comp.transition_to(LifecycleState.INITIALIZED)
        comp.transition_to(LifecycleState.RUNNING)
        assert comp.state == LifecycleState.RUNNING

    def test_initialized_to_stopped(self):
        comp = _StubComponent()
        comp.transition_to(LifecycleState.INITIALIZED)
        comp.transition_to(LifecycleState.STOPPED)
        assert comp.state == LifecycleState.STOPPED

    def test_initialized_to_error(self):
        comp = _StubComponent()
        comp.transition_to(LifecycleState.INITIALIZED)
        comp.transition_to(LifecycleState.ERROR)
        assert comp.state == LifecycleState.ERROR

    def test_running_to_stopped(self):
        comp = _StubComponent()
        comp.transition_to(LifecycleState.INITIALIZED)
        comp.transition_to(LifecycleState.RUNNING)
        comp.transition_to(LifecycleState.STOPPED)
        assert comp.state == LifecycleState.STOPPED

    def test_running_to_error(self):
        comp = _StubComponent()
        comp.transition_to(LifecycleState.INITIALIZED)
        comp.transition_to(LifecycleState.RUNNING)
        comp.transition_to(LifecycleState.ERROR)
        assert comp.state == LifecycleState.ERROR

    def test_stopped_to_initialized_restart(self):
        comp = _StubComponent()
        comp.transition_to(LifecycleState.INITIALIZED)
        comp.transition_to(LifecycleState.RUNNING)
        comp.transition_to(LifecycleState.STOPPED)
        comp.transition_to(LifecycleState.INITIALIZED)
        assert comp.state == LifecycleState.INITIALIZED

    def test_error_to_initialized_recovery(self):
        comp = _StubComponent()
        comp.transition_to(LifecycleState.ERROR)
        comp.transition_to(LifecycleState.INITIALIZED)
        assert comp.state == LifecycleState.INITIALIZED


# ---------------------------------------------------------------------------
# Invalid transitions
# ---------------------------------------------------------------------------

class TestInvalidTransitions:
    def test_uninitialized_to_running(self):
        comp = _StubComponent()
        with pytest.raises(RuntimeError, match="Invalid lifecycle transition"):
            comp.transition_to(LifecycleState.RUNNING)

    def test_uninitialized_to_stopped(self):
        comp = _StubComponent()
        with pytest.raises(RuntimeError, match="Invalid lifecycle transition"):
            comp.transition_to(LifecycleState.STOPPED)

    def test_running_to_initialized(self):
        comp = _StubComponent()
        comp.transition_to(LifecycleState.INITIALIZED)
        comp.transition_to(LifecycleState.RUNNING)
        with pytest.raises(RuntimeError, match="Invalid lifecycle transition"):
            comp.transition_to(LifecycleState.INITIALIZED)

    def test_stopped_to_running(self):
        comp = _StubComponent()
        comp.transition_to(LifecycleState.INITIALIZED)
        comp.transition_to(LifecycleState.RUNNING)
        comp.transition_to(LifecycleState.STOPPED)
        with pytest.raises(RuntimeError, match="Invalid lifecycle transition"):
            comp.transition_to(LifecycleState.RUNNING)

    def test_error_to_running(self):
        comp = _StubComponent()
        comp.transition_to(LifecycleState.ERROR)
        with pytest.raises(RuntimeError, match="Invalid lifecycle transition"):
            comp.transition_to(LifecycleState.RUNNING)


# ---------------------------------------------------------------------------
# LifecycleManager
# ---------------------------------------------------------------------------

class TestLifecycleManager:
    def test_start_all_initializes_and_runs(self):
        mgr = LifecycleManager()
        c1 = _StubComponent("a")
        c2 = _StubComponent("b")
        mgr.register(c1)
        mgr.register(c2)

        mgr.start_all()

        assert c1.state == LifecycleState.RUNNING
        assert c2.state == LifecycleState.RUNNING
        assert c1.started is True
        assert c2.started is True

    def test_stop_all_stops_in_reverse_order(self):
        mgr = LifecycleManager()
        c1 = _StubComponent("a")
        c2 = _StubComponent("b")
        c3 = _StubComponent("c")
        mgr.register(c1)
        mgr.register(c2)
        mgr.register(c3)
        mgr.start_all()

        stop_order: list[str] = []

        class _TrackingStub(Component):
            def __init__(self, name: str) -> None:
                super().__init__()
                self._name = name

            def start(self) -> None:
                pass

            def stop(self) -> None:
                stop_order.append(self._name)

        # Replace with tracking components
        mgr2 = LifecycleManager()
        t1 = _TrackingStub("a")
        t2 = _TrackingStub("b")
        t3 = _TrackingStub("c")
        mgr2.register(t1)
        mgr2.register(t2)
        mgr2.register(t3)
        mgr2.start_all()
        mgr2.stop_all()

        assert stop_order == ["c", "b", "a"]

    def test_stop_all_skips_non_running(self):
        mgr = LifecycleManager()
        c1 = _StubComponent("a")
        c2 = _StubComponent("b")
        mgr.register(c1)
        mgr.register(c2)
        # Only start c1 manually to INITIALIZED (not RUNNING)
        c1.transition_to(LifecycleState.INITIALIZED)
        c2.transition_to(LifecycleState.INITIALIZED)
        c2.transition_to(LifecycleState.RUNNING)

        mgr.stop_all()

        assert c1.state == LifecycleState.INITIALIZED  # not stopped
        assert c2.state == LifecycleState.STOPPED

    def test_components_property_returns_copy(self):
        mgr = LifecycleManager()
        c1 = _StubComponent()
        mgr.register(c1)
        comps = mgr.components
        assert comps == [c1]
        comps.clear()
        assert len(mgr.components) == 1  # original list unaffected

    def test_health_report(self):
        mgr = LifecycleManager()
        c1 = _StubComponent("alpha")
        c2 = _StubComponent("beta")
        mgr.register(c1)
        mgr.register(c2)
        mgr.start_all()

        report = mgr.health_report()
        assert len(report) == 2
        assert report[0]["component"] == "_StubComponent"
        assert report[0]["state"] == "RUNNING"
        assert report[0]["name"] == "alpha"
        assert report[1]["name"] == "beta"

    def test_health_report_uninitialized(self):
        mgr = LifecycleManager()
        c1 = _StubComponent("idle")
        mgr.register(c1)

        report = mgr.health_report()
        assert report[0]["state"] == "UNINITIALIZED"
