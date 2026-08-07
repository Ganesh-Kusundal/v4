"""Tests for ExecutionEngine graceful shutdown (Step 3.3)."""

from __future__ import annotations

from unittest.mock import MagicMock

from tradex_trading.execution.engine import ExecutionEngine
from tradex_trading.execution.fill_sources import SimulatedFillSource
from tradex_trading.reactive.bus import ReactiveBus


def _make_engine() -> ExecutionEngine:
    bus = ReactiveBus()
    return ExecutionEngine(bus=bus, fill_source=SimulatedFillSource())


def test_shutdown_can_be_called_without_error() -> None:
    engine = _make_engine()
    engine.shutdown()  # must not raise


def test_shutdown_activates_kill_switch() -> None:
    engine = _make_engine()
    assert not engine.kill_switch
    engine.shutdown()
    assert engine.kill_switch is True


def test_context_manager_calls_shutdown() -> None:
    bus = ReactiveBus()
    with ExecutionEngine(bus=bus, fill_source=SimulatedFillSource()) as engine:
        assert not engine.kill_switch
    # After exiting the context manager, kill switch must be active.
    assert engine.kill_switch is True


def test_shutdown_disposes_pipeline_subscription() -> None:
    engine = _make_engine()
    # The pipeline disposable is created during _setup_pipeline().
    disposable = engine._pipeline_disposable
    assert disposable is not None
    # Wrap dispose to verify it gets called.
    disposable.dispose = MagicMock()  # type: ignore[method-assign]
    engine.shutdown()
    disposable.dispose.assert_called_once()


def test_shutdown_is_idempotent() -> None:
    engine = _make_engine()
    engine.shutdown()
    engine.shutdown()  # second call must not raise
    assert engine.kill_switch is True
