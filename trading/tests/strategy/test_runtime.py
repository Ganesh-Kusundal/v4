"""Integration tests for StrategyRuntime lifecycle, snapshot, and restore.

Uses real StrategyArtifact and the minimal inline strategy stub below —
no mocks, no broker, no SQLite.
"""

from __future__ import annotations

import pytest

from tradex_trading.strategy.artifacts import (
    ApprovalState,
    StrategyArtifact,
    StrategyRestoreResult,
    StrategySnapshot,
)
from tradex_trading.strategy.runtime import StrategyLifecycle, StrategyRuntime


# ---------------------------------------------------------------------------
# Minimal strategy stub — implements only what the runtime needs to hold a ref
# ---------------------------------------------------------------------------

class _StubStrategy:
    strategy_id = "stub"
    version = "1.0.0"


def _make_runtime(strategy_id: str = "stub", version: str = "1.0.0") -> StrategyRuntime:
    artifact = StrategyArtifact(strategy_id, version)
    return StrategyRuntime(artifact, _StubStrategy())


# ---------------------------------------------------------------------------
# Initial state
# ---------------------------------------------------------------------------

def test_runtime_starts_in_created_state() -> None:
    rt = _make_runtime()
    assert rt.lifecycle is StrategyLifecycle.CREATED


# ---------------------------------------------------------------------------
# start() — CREATED → RUNNING
# ---------------------------------------------------------------------------

def test_start_transitions_to_running() -> None:
    rt = _make_runtime()
    rt.start()
    assert rt.lifecycle is StrategyLifecycle.RUNNING


def test_start_from_non_created_raises() -> None:
    rt = _make_runtime()
    rt.start()
    with pytest.raises(RuntimeError, match="cannot start"):
        rt.start()


# ---------------------------------------------------------------------------
# stop() — RUNNING|PAUSED → STOPPED
# ---------------------------------------------------------------------------

def test_stop_from_running_transitions_to_stopped() -> None:
    rt = _make_runtime()
    rt.start()
    rt.stop()
    assert rt.lifecycle is StrategyLifecycle.STOPPED


def test_stop_from_paused_transitions_to_stopped() -> None:
    rt = _make_runtime()
    rt.start()
    rt.pause()
    rt.stop()
    assert rt.lifecycle is StrategyLifecycle.STOPPED


def test_stop_from_created_raises() -> None:
    rt = _make_runtime()
    with pytest.raises(RuntimeError, match="cannot stop"):
        rt.stop()


# ---------------------------------------------------------------------------
# pause() / resume()
# ---------------------------------------------------------------------------

def test_pause_running_transitions_to_paused() -> None:
    rt = _make_runtime()
    rt.start()
    rt.pause()
    assert rt.lifecycle is StrategyLifecycle.PAUSED


def test_resume_paused_transitions_to_running() -> None:
    rt = _make_runtime()
    rt.start()
    rt.pause()
    rt.resume()
    assert rt.lifecycle is StrategyLifecycle.RUNNING


def test_pause_from_non_running_raises() -> None:
    rt = _make_runtime()
    with pytest.raises(RuntimeError, match="cannot pause"):
        rt.pause()


def test_resume_from_non_paused_raises() -> None:
    rt = _make_runtime()
    rt.start()
    with pytest.raises(RuntimeError, match="cannot resume"):
        rt.resume()


# ---------------------------------------------------------------------------
# fail() — idempotent
# ---------------------------------------------------------------------------

def test_fail_from_running_transitions_to_failed() -> None:
    rt = _make_runtime()
    rt.start()
    rt.fail("boom")
    assert rt.lifecycle is StrategyLifecycle.FAILED


def test_fail_is_idempotent_from_stopped() -> None:
    rt = _make_runtime()
    rt.start()
    rt.stop()
    rt.fail()  # already terminal — must not raise
    assert rt.lifecycle is StrategyLifecycle.STOPPED


def test_fail_is_idempotent_from_failed() -> None:
    rt = _make_runtime()
    rt.start()
    rt.fail()
    rt.fail()  # second call — no-op
    assert rt.lifecycle is StrategyLifecycle.FAILED


# ---------------------------------------------------------------------------
# reset() — STOPPED|FAILED → CREATED, clears state
# ---------------------------------------------------------------------------

def test_reset_from_stopped_returns_to_created() -> None:
    rt = _make_runtime()
    rt.start()
    rt.set_state("k", 42)
    rt.stop()
    rt.reset()
    assert rt.lifecycle is StrategyLifecycle.CREATED
    assert rt.get_state("k") is None


def test_reset_from_failed_returns_to_created() -> None:
    rt = _make_runtime()
    rt.start()
    rt.fail()
    rt.reset()
    assert rt.lifecycle is StrategyLifecycle.CREATED


def test_reset_from_running_raises() -> None:
    rt = _make_runtime()
    rt.start()
    with pytest.raises(RuntimeError, match="cannot reset"):
        rt.reset()


# ---------------------------------------------------------------------------
# Full round-trip: CREATED → RUNNING → PAUSED → RUNNING → STOPPED → CREATED
# ---------------------------------------------------------------------------

def test_full_lifecycle_round_trip() -> None:
    rt = _make_runtime()
    assert rt.lifecycle is StrategyLifecycle.CREATED
    rt.start()
    assert rt.lifecycle is StrategyLifecycle.RUNNING
    rt.pause()
    assert rt.lifecycle is StrategyLifecycle.PAUSED
    rt.resume()
    assert rt.lifecycle is StrategyLifecycle.RUNNING
    rt.stop()
    assert rt.lifecycle is StrategyLifecycle.STOPPED
    rt.reset()
    assert rt.lifecycle is StrategyLifecycle.CREATED


# ---------------------------------------------------------------------------
# State bag
# ---------------------------------------------------------------------------

def test_state_bag_get_set() -> None:
    rt = _make_runtime()
    rt.set_state("bar_count", 7)
    assert rt.get_state("bar_count") == 7


def test_state_bag_default() -> None:
    rt = _make_runtime()
    assert rt.get_state("missing") is None
    assert rt.get_state("missing", 0) == 0


# ---------------------------------------------------------------------------
# snapshot()
# ---------------------------------------------------------------------------

def test_snapshot_captures_current_state() -> None:
    rt = _make_runtime("my_strat", "2.0.0")
    rt.start()
    rt.set_state("pos", 100)
    snap = rt.snapshot()

    assert snap.strategy_id == "my_strat"
    assert snap.version == "2.0.0"
    assert snap.lifecycle == "running"
    assert snap.state == {"pos": 100}
    assert snap.artifact_id is not None


def test_snapshot_is_a_copy_not_a_reference() -> None:
    rt = _make_runtime()
    rt.start()
    rt.set_state("x", 1)
    snap = rt.snapshot()
    rt.set_state("x", 99)
    assert snap.state["x"] == 1  # snapshot was not mutated


def test_snapshot_is_immutable() -> None:
    rt = _make_runtime()
    snap = rt.snapshot()
    with pytest.raises((TypeError, AttributeError)):
        snap.strategy_id = "hacked"  # type: ignore[misc]


# ---------------------------------------------------------------------------
# restore()
# ---------------------------------------------------------------------------

def test_restore_round_trips_state_and_lifecycle() -> None:
    rt = _make_runtime()
    rt.start()
    rt.set_state("pos", 50)
    snap = rt.snapshot()

    rt2 = _make_runtime()
    result = rt2.restore(snap)
    assert result.success is True
    assert result.error is None
    assert rt2.lifecycle is StrategyLifecycle.RUNNING
    assert rt2.get_state("pos") == 50


def test_restore_refuses_wrong_strategy_id() -> None:
    snap = StrategySnapshot(strategy_id="other", version="1.0.0", lifecycle="running")
    rt = _make_runtime("mine", "1.0.0")
    result = rt.restore(snap)
    assert result.success is False
    assert "other" in result.error


def test_restore_refuses_wrong_version() -> None:
    snap = StrategySnapshot(strategy_id="stub", version="9.9.9", lifecycle="running")
    rt = _make_runtime("stub", "1.0.0")
    result = rt.restore(snap)
    assert result.success is False
    assert "9.9.9" in result.error


def test_restore_refuses_unknown_lifecycle() -> None:
    snap = StrategySnapshot(strategy_id="stub", version="1.0.0", lifecycle="BOGUS")
    rt = _make_runtime()
    result = rt.restore(snap)
    assert result.success is False
    assert "BOGUS" in result.error


def test_restore_leaves_runtime_unchanged_on_failure() -> None:
    bad_snap = StrategySnapshot(strategy_id="wrong", version="1.0.0", lifecycle="running")
    rt = _make_runtime("stub", "1.0.0")
    rt.restore(bad_snap)
    assert rt.lifecycle is StrategyLifecycle.CREATED  # unchanged


# ---------------------------------------------------------------------------
# StrategySnapshot validation
# ---------------------------------------------------------------------------

def test_snapshot_requires_strategy_id() -> None:
    with pytest.raises(ValueError, match="strategy_id"):
        StrategySnapshot(strategy_id="  ", version="1.0.0", lifecycle="created")


# ---------------------------------------------------------------------------
# StrategyRestoreResult
# ---------------------------------------------------------------------------

def test_restore_result_success() -> None:
    r = StrategyRestoreResult(success=True)
    assert r.success is True
    assert r.error is None


def test_restore_result_failure() -> None:
    r = StrategyRestoreResult(success=False, error="oops")
    assert r.success is False
    assert r.error == "oops"
