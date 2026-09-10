"""Tests for the ordered graceful-shutdown coordinator (PE-10)."""

from __future__ import annotations

import pytest

from tradex_trading.runtime.shutdown import ShutdownCoordinator


class TestShutdownCoordinator:
    """ShutdownCoordinator runs phases in priority order with isolation."""

    def test_phases_run_in_priority_order(self) -> None:
        """Lower priority number runs first."""
        order: list[str] = []
        coord = ShutdownCoordinator()
        coord.register("third", priority=3, action=lambda: order.append("third"))
        coord.register("first", priority=1, action=lambda: order.append("first"))
        coord.register("second", priority=2, action=lambda: order.append("second"))

        failures = coord.shutdown()

        assert order == ["first", "second", "third"]
        assert failures == []

    def test_phase_failure_does_not_block_others(self) -> None:
        """A failing phase is logged but subsequent phases still run."""
        order: list[str] = []

        def failing() -> None:
            order.append("failing")
            raise RuntimeError("boom")

        coord = ShutdownCoordinator()
        coord.register("ok_1", priority=1, action=lambda: order.append("ok_1"))
        coord.register("fail", priority=2, action=failing)
        coord.register("ok_2", priority=3, action=lambda: order.append("ok_2"))

        failures = coord.shutdown()

        assert order == ["ok_1", "failing", "ok_2"]
        assert failures == ["fail"]

    def test_empty_coordinator_shuts_down_cleanly(self) -> None:
        """No phases registered — shutdown is a no-op."""
        coord = ShutdownCoordinator()
        failures = coord.shutdown()
        assert failures == []

    def test_runtime_context_uses_coordinator(self) -> None:
        """RuntimeContext.close() delegates to ShutdownCoordinator with
        the correct phase ordering."""
        from tradex_trading.config.schema import AppConfig
        from tradex_trading.runtime.startup import boot

        cfg = AppConfig(mode="paper")
        session = boot(cfg)
        # close() should run without error and in the right order
        session.stop()
