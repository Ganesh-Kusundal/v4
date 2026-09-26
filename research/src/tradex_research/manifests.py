"""Research run manifests — fully deterministic records of every input to a research run.

Each manifest is a frozen, hashable dataclass whose ``manifest_hash`` / ``universe_hash``
is a stable SHA-256 prefix over its content, so two manifests built from the same
logical inputs always produce the same hash regardless of construction order.

Manifest hierarchy (bottom-up dependency):
  DatasetManifest  — raw/adjusted OHLCV slice (symbols, timeframe, dates, hash)
  UniverseManifest — constituent list as-of a point in time (PIT survivorship guard)
  FeatureManifest  — feature names + version + ts-column names (PIT column contract)
  CostModelManifest — cost assumptions (commission, slippage, impact, borrow)
  SimulationManifest — engine version + capital + position sizing
  PromotionManifest — ties all of the above into an approval record
"""

from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass, field
from enum import StrEnum
from datetime import date, datetime, UTC
from typing import Any

class ApprovalState(StrEnum):
    DRAFT = "draft"
    APPROVED = "approved"
    RETIRED = "retired"


# Local copy so tradex-research has no trading dependency (strangler wave 1).


def _sha(payload: dict) -> str:
    """Stable 24-char SHA-256 prefix over a JSON-serialised dict."""
    raw = json.dumps(payload, sort_keys=True, separators=(",", ":"), default=str)
    return hashlib.sha256(raw.encode()).hexdigest()[:24]


@dataclass(frozen=True, slots=True)
class DatasetManifest:
    """Records the exact data snapshot used in a research or backtest run.

    ``price_series`` must be one of ``"raw"`` or ``"adjusted"`` — the single
    source of truth that prevents silently mixing the two in a pipeline.

    ``corporate_action_cutoff``: adjustments with ex_date strictly after this
    date were NOT yet known at research time and must not be applied.  None
    means "no cutoff recorded" (use with caution for historical replays).
    """

    symbols: tuple[str, ...]
    timeframe: str
    start_date: date
    end_date: date
    data_hash: str
    price_series: str  # "raw" | "adjusted"
    corporate_action_cutoff: date | None = None

    def __post_init__(self) -> None:
        if self.price_series not in ("raw", "adjusted"):
            raise ValueError(
                f"price_series must be 'raw' or 'adjusted', got {self.price_series!r}"
            )
        if self.start_date > self.end_date:
            raise ValueError(
                f"start_date ({self.start_date}) must not exceed end_date ({self.end_date})"
            )

    @property
    def manifest_hash(self) -> str:
        return _sha(
            {
                "symbols": sorted(self.symbols),
                "timeframe": self.timeframe,
                "start_date": self.start_date.isoformat(),
                "end_date": self.end_date.isoformat(),
                "data_hash": self.data_hash,
                "price_series": self.price_series,
                "corporate_action_cutoff": (
                    self.corporate_action_cutoff.isoformat()
                    if self.corporate_action_cutoff
                    else None
                ),
            }
        )


@dataclass(frozen=True, slots=True)
class UniverseManifest:
    """Records the universe composition as of a specific point in time.

    Prevents survivorship bias: a research run must use the universe that
    existed at ``as_of_date``, not the current (post-event) universe.
    ``assert_historical_universe`` in ``pit.py`` enforces this at run time.
    """

    universe_name: str
    as_of_date: date
    symbols: tuple[str, ...]

    def __post_init__(self) -> None:
        if not self.universe_name.strip():
            raise ValueError("universe_name is required")
        if not self.symbols:
            raise ValueError("universe must contain at least one symbol")

    @property
    def universe_hash(self) -> str:
        return _sha(
            {
                "universe_name": self.universe_name,
                "as_of_date": self.as_of_date.isoformat(),
                "symbols": sorted(self.symbols),
            }
        )


@dataclass(frozen=True, slots=True)
class FeatureManifest:
    """Records the feature definitions used in a research run.

    ``feature_ts_column`` names the column holding the timestamp of the last
    bar used to compute the feature; ``decision_ts_column`` holds the timestamp
    at which the signal fires.  ``assert_no_future_candle`` requires
    ``feature_ts_column <= decision_ts_column`` for every row.
    """

    feature_names: tuple[str, ...]
    feature_version: str
    feature_ts_column: str
    decision_ts_column: str

    def __post_init__(self) -> None:
        if not self.feature_names:
            raise ValueError("feature_names must not be empty")
        if not self.feature_version.strip():
            raise ValueError("feature_version is required")
        if self.feature_ts_column == self.decision_ts_column:
            raise ValueError(
                "feature_ts_column and decision_ts_column must differ; "
                "they name different roles in the PIT contract"
            )

    @property
    def manifest_hash(self) -> str:
        return _sha(
            {
                "feature_names": sorted(self.feature_names),
                "feature_version": self.feature_version,
                "feature_ts_column": self.feature_ts_column,
                "decision_ts_column": self.decision_ts_column,
            }
        )


@dataclass(frozen=True, slots=True)
class CostModelManifest:
    """Records the cost assumptions used in a simulation.

    All components in basis points (bps = 0.01%).  ``total_round_trip_bps``
    is the full in+out cost the simulation uses as a hurdle.
    """

    commission_bps: float
    slippage_bps: float
    impact_bps: float
    borrow_cost_bps: float = 0.0  # per-year short-sell borrow cost

    def __post_init__(self) -> None:
        if self.commission_bps < 0 or self.slippage_bps < 0 or self.impact_bps < 0:
            raise ValueError("cost components must be non-negative")

    @property
    def total_round_trip_bps(self) -> float:
        """Full round-trip cost (entry + exit) in basis points."""
        return 2.0 * (self.commission_bps + self.slippage_bps + self.impact_bps)

    @property
    def manifest_hash(self) -> str:
        return _sha(
            {
                "commission_bps": self.commission_bps,
                "slippage_bps": self.slippage_bps,
                "impact_bps": self.impact_bps,
                "borrow_cost_bps": self.borrow_cost_bps,
            }
        )


@dataclass(frozen=True, slots=True)
class SimulationManifest:
    """Records simulation engine parameters for full reproducibility."""

    engine_version: str
    initial_capital: float
    position_sizing: str  # "fixed" | "kelly" | "equal_weight" | etc.
    max_position_pct: float = 0.05  # max single position as fraction of capital
    parameters: dict[str, Any] = field(default_factory=dict)

    def __post_init__(self) -> None:
        if self.initial_capital <= 0:
            raise ValueError("initial_capital must be positive")
        if not 0.0 < self.max_position_pct <= 1.0:
            raise ValueError("max_position_pct must be in (0, 1]")


@dataclass(frozen=True, slots=True)
class PromotionManifest:
    """Records the approval of a research run for production use.

    Ties together the hashes of all manifests that characterise the run, plus
    the result hash, so every promotion is fully auditable and reproducible.
    An ``APPROVED`` promotion requires ``approved_by``, ``approved_at``, and a
    known ``code_revision`` — the same bar ``StrategyArtifact`` sets.
    """

    experiment_id: str
    dataset_manifest_hash: str
    universe_manifest_hash: str
    feature_manifest_hash: str
    cost_manifest_hash: str
    result_hash: str
    code_revision: str
    approval: ApprovalState = ApprovalState.DRAFT
    approved_by: str = ""
    approved_at: datetime | None = None

    def __post_init__(self) -> None:
        if not self.experiment_id.strip():
            raise ValueError("experiment_id is required")
        if self.approval == ApprovalState.APPROVED:
            if not self.approved_by.strip():
                raise ValueError("approved_by is required for APPROVED state")
            if self.approved_at is None:
                raise ValueError("approved_at is required for APPROVED state")
            if not self.code_revision.strip() or self.code_revision == "unknown":
                raise ValueError(
                    "code_revision is required for APPROVED state (got 'unknown')"
                )


__all__ = [
    "ApprovalState",
    "CostModelManifest",
    "DatasetManifest",
    "FeatureManifest",
    "PromotionManifest",
    "SimulationManifest",
    "UniverseManifest",
]
