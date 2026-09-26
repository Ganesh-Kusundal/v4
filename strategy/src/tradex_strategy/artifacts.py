"""Versioned strategy and research artifacts."""

from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass, field
from datetime import UTC, datetime
from enum import StrEnum
from typing import Any, Protocol


class ApprovalState(StrEnum):
    DRAFT = "draft"
    APPROVED = "approved"
    RETIRED = "retired"


@dataclass(frozen=True, slots=True)
class StrategyArtifact:
    strategy_id: str
    version: str
    parameters: dict[str, Any] = field(default_factory=dict)
    feature_version: str = "unversioned"
    dataset_version: str = "unversioned"
    universe_version: str = "unversioned"
    execution_policy_version: str = "unversioned"
    code_revision: str = "unknown"
    approval: ApprovalState = ApprovalState.DRAFT

    def __post_init__(self) -> None:
        if not self.strategy_id.strip() or not self.version.strip():
            raise ValueError("strategy artifact requires strategy_id and version")
        if self.approval is ApprovalState.APPROVED and self.code_revision == "unknown":
            raise ValueError("approved strategy artifact requires a code revision")

    @property
    def artifact_id(self) -> str:
        payload = json.dumps(
            {
                "strategy_id": self.strategy_id,
                "version": self.version,
                "parameters": self.parameters,
                "feature_version": self.feature_version,
                "dataset_version": self.dataset_version,
                "universe_version": self.universe_version,
                "execution_policy_version": self.execution_policy_version,
                "code_revision": self.code_revision,
            },
            sort_keys=True,
            separators=(",", ":"),
        )
        return hashlib.sha256(payload.encode("utf-8")).hexdigest()[:24]


class StrategyArtifactStore(Protocol):
    def publish(self, artifact: StrategyArtifact) -> str: ...
    def load(self, artifact_id: str) -> StrategyArtifact: ...


@dataclass(frozen=True, slots=True)
class ExperimentManifest:
    experiment_id: str
    dataset_hash: str
    universe_hash: str
    feature_version: str
    label_definition: str
    split_definition: str
    parameters: dict[str, Any]
    search_budget: int
    random_seed: int
    code_revision: str
    cost_assumptions: dict[str, Any] = field(default_factory=dict)
    created_at: datetime = field(default_factory=lambda: datetime.now(UTC))

    def __post_init__(self) -> None:
        if not self.experiment_id.strip():
            raise ValueError("experiment_id is required")
        if self.search_budget <= 0:
            raise ValueError("search_budget must be positive")


@dataclass(frozen=True, slots=True)
class StrategySnapshot:
    """Point-in-time snapshot of a strategy runtime's mutable state.

    Captured by ``StrategyRuntime.snapshot()`` and consumed by
    ``StrategyRuntime.restore()``.  Frozen so it is safe to pass around
    and store without defensive copying.
    """

    strategy_id: str
    version: str
    lifecycle: str  # StrategyLifecycle value at capture time
    state: dict[str, Any] = field(default_factory=dict)
    artifact_id: str | None = None
    created_at: datetime = field(default_factory=lambda: datetime.now(UTC))

    def __post_init__(self) -> None:
        if not self.strategy_id.strip():
            raise ValueError("StrategySnapshot requires strategy_id")


@dataclass(frozen=True, slots=True)
class StrategyRestoreResult:
    """Result returned by ``StrategyRuntime.restore(snapshot)``."""

    success: bool
    error: str | None = None


class InMemoryStrategyArtifactStore:
    """Simple deterministic artifact store for tests and local runtime."""

    def __init__(self) -> None:
        self._items: dict[str, StrategyArtifact] = {}

    def publish(self, artifact: StrategyArtifact) -> str:
        self._items[artifact.artifact_id] = artifact
        return artifact.artifact_id

    def load(self, artifact_id: str) -> StrategyArtifact:
        return self._items[artifact_id]


