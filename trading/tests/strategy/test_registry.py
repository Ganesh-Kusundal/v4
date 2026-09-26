"""Integration tests for StrategyRegistry.

Real StrategyRuntime instances; no mocks, no broker, no SQLite.
"""

from __future__ import annotations

import pytest

from tradex_trading.strategy.artifacts import StrategyArtifact
from tradex_trading.strategy.registry import StrategyRegistry
from tradex_trading.strategy.runtime import StrategyLifecycle, StrategyRuntime


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

class _Stub:
    def __init__(self, sid: str) -> None:
        self.strategy_id = sid
        self.version = "1.0.0"


def _rt(strategy_id: str = "s1") -> StrategyRuntime:
    return StrategyRuntime(StrategyArtifact(strategy_id, "1.0.0"), _Stub(strategy_id))


# ---------------------------------------------------------------------------
# register / unregister / get / list_all
# ---------------------------------------------------------------------------

def test_registry_starts_empty() -> None:
    reg = StrategyRegistry()
    assert reg.list_all() == []
    assert len(reg) == 0


def test_register_and_get() -> None:
    reg = StrategyRegistry()
    rt = _rt("alpha")
    reg.register(rt)
    assert reg.get("alpha") is rt


def test_register_duplicate_raises() -> None:
    reg = StrategyRegistry()
    reg.register(_rt("alpha"))
    with pytest.raises(KeyError, match="alpha"):
        reg.register(_rt("alpha"))


def test_unregister_returns_runtime() -> None:
    reg = StrategyRegistry()
    rt = _rt("beta")
    reg.register(rt)
    removed = reg.unregister("beta")
    assert removed is rt
    assert reg.get("beta") is None


def test_unregister_missing_returns_none() -> None:
    reg = StrategyRegistry()
    assert reg.unregister("nonexistent") is None


def test_list_all_returns_all_runtimes() -> None:
    reg = StrategyRegistry()
    rts = [_rt("a"), _rt("b"), _rt("c")]
    for rt in rts:
        reg.register(rt)
    listed = reg.list_all()
    assert len(listed) == 3
    assert set(r.artifact.strategy_id for r in listed) == {"a", "b", "c"}


def test_list_all_returns_copy() -> None:
    reg = StrategyRegistry()
    reg.register(_rt("x"))
    lst = reg.list_all()
    lst.clear()
    assert len(reg) == 1  # original registry unaffected


# ---------------------------------------------------------------------------
# start_all / stop_all
# ---------------------------------------------------------------------------

def test_start_all_starts_all_created_runtimes() -> None:
    reg = StrategyRegistry()
    for sid in ("a", "b", "c"):
        reg.register(_rt(sid))
    reg.start_all()
    assert all(rt.lifecycle is StrategyLifecycle.RUNNING for rt in reg.list_all())


def test_start_all_skips_already_running() -> None:
    reg = StrategyRegistry()
    rt = _rt("a")
    reg.register(rt)
    rt.start()  # already RUNNING
    reg.start_all()  # must not raise
    assert rt.lifecycle is StrategyLifecycle.RUNNING


def test_stop_all_stops_running_runtimes() -> None:
    reg = StrategyRegistry()
    for sid in ("a", "b"):
        rt = _rt(sid)
        reg.register(rt)
        rt.start()
    reg.stop_all()
    assert all(rt.lifecycle is StrategyLifecycle.STOPPED for rt in reg.list_all())


def test_stop_all_stops_paused_runtimes() -> None:
    reg = StrategyRegistry()
    rt = _rt("p")
    reg.register(rt)
    rt.start()
    rt.pause()
    reg.stop_all()
    assert rt.lifecycle is StrategyLifecycle.STOPPED


def test_stop_all_skips_created_runtimes() -> None:
    reg = StrategyRegistry()
    rt = _rt("c")
    reg.register(rt)
    reg.stop_all()  # must not raise — CREATED is not stoppable
    assert rt.lifecycle is StrategyLifecycle.CREATED


def test_start_then_stop_all_lifecycle() -> None:
    reg = StrategyRegistry()
    for sid in ("x", "y", "z"):
        reg.register(_rt(sid))
    reg.start_all()
    reg.stop_all()
    assert all(rt.lifecycle is StrategyLifecycle.STOPPED for rt in reg.list_all())


# ---------------------------------------------------------------------------
# Independent runtimes do not share state
# ---------------------------------------------------------------------------

def test_runtimes_are_independent() -> None:
    reg = StrategyRegistry()
    rt_a = _rt("a")
    rt_b = _rt("b")
    reg.register(rt_a)
    reg.register(rt_b)

    rt_a.start()
    rt_a.set_state("counter", 5)

    assert rt_b.lifecycle is StrategyLifecycle.CREATED
    assert rt_b.get_state("counter") is None
