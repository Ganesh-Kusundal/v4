"""Wave C4 — integration: strategy lifecycle wired through boot.

Verifies that startup.py's strategy block:
  1. Creates a StrategyRegistry and wraps every auto-discovered extension
     strategy in a StrategyRuntime (CREATED after registration).
  2. Calls registry.start_all() after session.start() → all runtimes RUNNING.
  3. Calls registry.stop_all() inside session.stop() → all runtimes STOPPED.

No broker, no SQLite.  paper mode only.
"""

from __future__ import annotations

from tradex_trading.config.schema import AppConfig
from tradex_trading.runtime.startup import boot
from tradex_trading.strategy.extensions import all_strategies
from tradex_trading.strategy.registry import StrategyRegistry
from tradex_trading.strategy.runtime import StrategyLifecycle

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _boot_paper(wire: bool = True):
    """Boot a paper session; caller is responsible for session.stop().

    Examples are opt-in (``execution.auto_register_examples``) so a boot never
    trades something nobody deployed. These tests verify the discovery wiring
    itself, so they ask for the examples explicitly.
    """
    from dataclasses import replace

    config = AppConfig(mode="paper")
    config = replace(
        config,
        execution=replace(config.execution, auto_register_examples=True),
    )
    return boot(config, wire_strategies=wire)


# ---------------------------------------------------------------------------
# C4-1: registry is created and populated when wire_strategies=True
# ---------------------------------------------------------------------------

def test_strategy_registry_is_wired_into_session() -> None:
    session = _boot_paper(wire=True)
    try:
        reg = session.strategy_registry()
        assert reg is not None, (
            "boot() with wire_strategies=True must expose strategy_registry() on the session"
        )
        assert isinstance(reg, StrategyRegistry)
    finally:
        session.stop()


def test_registry_contains_all_discovered_strategies() -> None:
    session = _boot_paper(wire=True)
    try:
        reg = session.strategy_registry()
        assert len(reg) == len(all_strategies), (
            f"registry must hold one StrategyRuntime per auto-discovered strategy; "
            f"expected {len(all_strategies)}, got {len(reg)}"
        )
    finally:
        session.stop()


def test_strategy_ids_match_extension_strategies() -> None:
    session = _boot_paper(wire=True)
    try:
        reg = session.strategy_registry()
        registered_ids = {rt.artifact.strategy_id for rt in reg.list_all()}
        expected_ids = {s.strategy_id for s in all_strategies}
        assert registered_ids == expected_ids
    finally:
        session.stop()


# ---------------------------------------------------------------------------
# C4-2: runtimes are RUNNING immediately after session.start()
# ---------------------------------------------------------------------------

def test_all_runtimes_are_running_after_boot() -> None:
    session = _boot_paper(wire=True)
    try:
        reg = session.strategy_registry()
        runtimes = reg.list_all()
        assert runtimes, "no runtimes registered — expected at least one from extensions"
        non_running = [
            (rt.artifact.strategy_id, rt.lifecycle)
            for rt in runtimes
            if rt.lifecycle is not StrategyLifecycle.RUNNING
        ]
        assert not non_running, (
            f"all runtimes must be RUNNING after session.start(); "
            f"these were not: {non_running}"
        )
    finally:
        session.stop()


# ---------------------------------------------------------------------------
# C4-3: runtimes are STOPPED after session.stop()
# ---------------------------------------------------------------------------

def test_all_runtimes_are_stopped_after_session_stop() -> None:
    session = _boot_paper(wire=True)
    reg = session.strategy_registry()
    session.stop()

    runtimes = reg.list_all()
    non_stopped = [
        (rt.artifact.strategy_id, rt.lifecycle)
        for rt in runtimes
        if rt.lifecycle is not StrategyLifecycle.STOPPED
    ]
    assert not non_stopped, (
        f"all runtimes must be STOPPED after session.stop(); "
        f"these were not: {non_stopped}"
    )


def test_stop_is_idempotent_with_registry() -> None:
    """Calling session.stop() twice must not raise (registry stop_all is safe)."""
    session = _boot_paper(wire=True)
    session.stop()
    session.stop()  # second call must be a no-op


# ---------------------------------------------------------------------------
# C4-4: wire_strategies=False → no registry
# ---------------------------------------------------------------------------

def test_no_registry_when_wire_strategies_false() -> None:
    session = _boot_paper(wire=False)
    try:
        reg = session.strategy_registry()
        assert reg is None, (
            "wire_strategies=False must NOT attach _strategy_registry"
        )
    finally:
        session.stop()


# ---------------------------------------------------------------------------
# C4-5: registry stop does not interfere with bus/broker teardown
# ---------------------------------------------------------------------------

def test_session_stop_fully_completes_with_registry() -> None:
    """session.stop() must complete without raising even when registry is wired."""
    from tradex_trading.sdk.session import SessionState

    session = _boot_paper(wire=True)
    session.stop()
    assert session.state == SessionState.STOPPED
