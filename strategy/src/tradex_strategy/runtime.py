"""Strategy lifecycle runtime.

``StrategyRuntime`` wraps a Strategy protocol instance and manages its
lifecycle state machine:

    CREATED → STARTING → RUNNING → PAUSED → RUNNING
                                 → STOPPING → STOPPED → (reset) → CREATED
    any non-terminal → FAILED → (reset) → CREATED

Strategies MUST NOT access broker, SQLite, or hidden globals.  The runtime
enforces this contract by owning all I/O surfaces; the strategy itself is
a pure event-handler that reads ``StrategyContext`` and emits ``Signal``.
"""

from __future__ import annotations

from enum import StrEnum
from typing import Any

from tradex_strategy.artifacts import (
    StrategyArtifact,
    StrategyRestoreResult,
    StrategySnapshot,
)


class StrategyLifecycle(StrEnum):
    CREATED = "created"
    STARTING = "starting"
    RUNNING = "running"
    PAUSED = "paused"
    STOPPING = "stopping"
    STOPPED = "stopped"
    FAILED = "failed"


# States from which the strategy may be started.
_STARTABLE = frozenset({StrategyLifecycle.CREATED})
# States from which the strategy may be stopped.
_STOPPABLE = frozenset({StrategyLifecycle.RUNNING, StrategyLifecycle.PAUSED})
# Terminal states that allow a reset back to CREATED.
_RESETTABLE = frozenset({StrategyLifecycle.STOPPED, StrategyLifecycle.FAILED})


class StrategyRuntime:
    """Lifecycle manager for one strategy instance.

    The runtime owns:
    - the ``StrategyArtifact`` that describes the strategy version/params
    - the mutable ``_state`` dict that the strategy may read/write across bars
    - the ``StrategyLifecycle`` state machine

    The ``ReactiveStrategyEngine`` calls ``on_bar`` / ``on_quote`` etc;
    ``StrategyRuntime`` does not call those hooks itself — it provides the
    lifecycle envelope so the engine can gate event delivery to only RUNNING
    strategies.
    """

    def __init__(self, artifact: StrategyArtifact, strategy: Any) -> None:
        self._artifact = artifact
        self._strategy = strategy
        self._lifecycle: StrategyLifecycle = StrategyLifecycle.CREATED
        self._state: dict[str, Any] = {}

    # ------------------------------------------------------------------
    # Properties
    # ------------------------------------------------------------------

    @property
    def lifecycle(self) -> StrategyLifecycle:
        return self._lifecycle

    @property
    def artifact(self) -> StrategyArtifact:
        return self._artifact

    @property
    def strategy(self) -> Any:
        return self._strategy

    # ------------------------------------------------------------------
    # Lifecycle transitions
    # ------------------------------------------------------------------

    def start(self) -> None:
        """Transition CREATED → STARTING → RUNNING.

        Raises ``RuntimeError`` if the current state is not CREATED.
        Transitions to FAILED on unexpected exceptions (should not normally
        occur in the synchronous path, but kept for completeness).
        """
        if self._lifecycle not in _STARTABLE:
            raise RuntimeError(
                f"cannot start strategy {self._artifact.strategy_id!r} "
                f"in {self._lifecycle} state (must be CREATED)"
            )
        self._lifecycle = StrategyLifecycle.STARTING
        try:
            # Synchronous path: no async broker I/O here.
            # Actual on_start() is called by ReactiveStrategyEngine.
            self._lifecycle = StrategyLifecycle.RUNNING
        except Exception:
            self._lifecycle = StrategyLifecycle.FAILED
            raise

    def stop(self) -> None:
        """Transition RUNNING|PAUSED → STOPPING → STOPPED.

        Raises ``RuntimeError`` if the strategy is not in a stoppable state.
        """
        if self._lifecycle not in _STOPPABLE:
            raise RuntimeError(
                f"cannot stop strategy {self._artifact.strategy_id!r} "
                f"in {self._lifecycle} state (must be RUNNING or PAUSED)"
            )
        self._lifecycle = StrategyLifecycle.STOPPING
        try:
            self._lifecycle = StrategyLifecycle.STOPPED
        except Exception:
            self._lifecycle = StrategyLifecycle.FAILED
            raise

    def pause(self) -> None:
        """Transition RUNNING → PAUSED.

        Raises ``RuntimeError`` if the strategy is not RUNNING.
        """
        if self._lifecycle is not StrategyLifecycle.RUNNING:
            raise RuntimeError(
                f"cannot pause strategy {self._artifact.strategy_id!r} "
                f"in {self._lifecycle} state (must be RUNNING)"
            )
        self._lifecycle = StrategyLifecycle.PAUSED

    def resume(self) -> None:
        """Transition PAUSED → RUNNING.

        Raises ``RuntimeError`` if the strategy is not PAUSED.
        """
        if self._lifecycle is not StrategyLifecycle.PAUSED:
            raise RuntimeError(
                f"cannot resume strategy {self._artifact.strategy_id!r} "
                f"in {self._lifecycle} state (must be PAUSED)"
            )
        self._lifecycle = StrategyLifecycle.RUNNING

    def fail(self, reason: str = "") -> None:
        """Force the runtime into FAILED from any non-terminal state.

        Used by supervising components when an unrecoverable error is detected
        (e.g. the strategy raised inside on_bar).
        """
        if self._lifecycle in (StrategyLifecycle.STOPPED, StrategyLifecycle.FAILED):
            return  # already terminal — idempotent
        self._lifecycle = StrategyLifecycle.FAILED

    def reset(self) -> None:
        """Transition STOPPED|FAILED → CREATED and wipe accumulated state.

        Raises ``RuntimeError`` if the strategy is still live.
        """
        if self._lifecycle not in _RESETTABLE:
            raise RuntimeError(
                f"cannot reset strategy {self._artifact.strategy_id!r} "
                f"in {self._lifecycle} state (must be STOPPED or FAILED)"
            )
        self._state.clear()
        self._lifecycle = StrategyLifecycle.CREATED

    # ------------------------------------------------------------------
    # Snapshot / restore
    # ------------------------------------------------------------------

    def snapshot(self) -> StrategySnapshot:
        """Capture current lifecycle and state into an immutable snapshot."""
        return StrategySnapshot(
            strategy_id=self._artifact.strategy_id,
            version=self._artifact.version,
            lifecycle=str(self._lifecycle),
            state=dict(self._state),
            artifact_id=self._artifact.artifact_id,
        )

    def restore(self, snapshot: StrategySnapshot) -> StrategyRestoreResult:
        """Restore state from a previously captured snapshot.

        Identity checks (strategy_id, version) must match the current
        artifact.  On mismatch the restore is refused and the runtime
        is left unchanged.
        """
        if snapshot.strategy_id != self._artifact.strategy_id:
            return StrategyRestoreResult(
                success=False,
                error=(
                    f"snapshot strategy_id {snapshot.strategy_id!r} does not "
                    f"match runtime {self._artifact.strategy_id!r}"
                ),
            )
        if snapshot.version != self._artifact.version:
            return StrategyRestoreResult(
                success=False,
                error=(
                    f"snapshot version {snapshot.version!r} does not "
                    f"match runtime {self._artifact.version!r}"
                ),
            )
        try:
            lifecycle = StrategyLifecycle(snapshot.lifecycle)
        except ValueError:
            return StrategyRestoreResult(
                success=False,
                error=f"unknown lifecycle value {snapshot.lifecycle!r} in snapshot",
            )
        self._state = dict(snapshot.state)
        self._lifecycle = lifecycle
        return StrategyRestoreResult(success=True)

    # ------------------------------------------------------------------
    # State bag helpers (used by strategy context bridge)
    # ------------------------------------------------------------------

    def set_state(self, key: str, value: Any) -> None:
        """Write a key into the mutable strategy state bag."""
        self._state[key] = value

    def get_state(self, key: str, default: Any = None) -> Any:
        """Read a key from the mutable strategy state bag."""
        return self._state.get(key, default)


