"""Research manifest contract tests (TDD — written before implementation)."""

from __future__ import annotations

from datetime import date, datetime, UTC

import pytest

from tradex_research.manifests import (
    CostModelManifest,
    DatasetManifest,
    FeatureManifest,
    PromotionManifest,
    SimulationManifest,
    UniverseManifest,
)
from tradex_research.manifests import ApprovalState


# ---------------------------------------------------------------------------
# DatasetManifest
# ---------------------------------------------------------------------------


def test_dataset_manifest_rejects_unknown_price_series() -> None:
    with pytest.raises(ValueError, match="price_series"):
        DatasetManifest(
            symbols=("RELIANCE",),
            timeframe="1m",
            start_date=date(2026, 1, 1),
            end_date=date(2026, 3, 31),
            data_hash="abc123",
            price_series="unknown",
        )


def test_dataset_manifest_rejects_inverted_dates() -> None:
    with pytest.raises(ValueError, match="start_date"):
        DatasetManifest(
            symbols=("RELIANCE",),
            timeframe="1m",
            start_date=date(2026, 4, 1),
            end_date=date(2026, 3, 31),
            data_hash="abc123",
            price_series="raw",
        )


def test_dataset_manifest_hash_stable_for_same_inputs() -> None:
    a = DatasetManifest(
        symbols=("RELIANCE", "TCS"),
        timeframe="1m",
        start_date=date(2026, 1, 1),
        end_date=date(2026, 3, 31),
        data_hash="d1",
        price_series="adjusted",
    )
    b = DatasetManifest(
        symbols=("TCS", "RELIANCE"),  # different order — must produce same hash
        timeframe="1m",
        start_date=date(2026, 1, 1),
        end_date=date(2026, 3, 31),
        data_hash="d1",
        price_series="adjusted",
    )
    assert a.manifest_hash == b.manifest_hash


def test_dataset_manifest_hash_differs_for_different_price_series() -> None:
    raw = DatasetManifest(
        symbols=("RELIANCE",),
        timeframe="1m",
        start_date=date(2026, 1, 1),
        end_date=date(2026, 3, 31),
        data_hash="d1",
        price_series="raw",
    )
    adjusted = DatasetManifest(
        symbols=("RELIANCE",),
        timeframe="1m",
        start_date=date(2026, 1, 1),
        end_date=date(2026, 3, 31),
        data_hash="d1",
        price_series="adjusted",
    )
    assert raw.manifest_hash != adjusted.manifest_hash


def test_dataset_manifest_corporate_action_cutoff_included_in_hash() -> None:
    without_cutoff = DatasetManifest(
        symbols=("RELIANCE",),
        timeframe="1m",
        start_date=date(2026, 1, 1),
        end_date=date(2026, 3, 31),
        data_hash="d1",
        price_series="raw",
    )
    with_cutoff = DatasetManifest(
        symbols=("RELIANCE",),
        timeframe="1m",
        start_date=date(2026, 1, 1),
        end_date=date(2026, 3, 31),
        data_hash="d1",
        price_series="raw",
        corporate_action_cutoff=date(2026, 3, 31),
    )
    assert without_cutoff.manifest_hash != with_cutoff.manifest_hash


# ---------------------------------------------------------------------------
# UniverseManifest
# ---------------------------------------------------------------------------


def test_universe_manifest_rejects_empty_name() -> None:
    with pytest.raises(ValueError, match="universe_name"):
        UniverseManifest(
            universe_name="  ",
            as_of_date=date(2026, 1, 1),
            symbols=("RELIANCE",),
        )


def test_universe_manifest_rejects_empty_symbols() -> None:
    with pytest.raises(ValueError, match="at least one symbol"):
        UniverseManifest(
            universe_name="nifty50",
            as_of_date=date(2026, 1, 1),
            symbols=(),
        )


def test_universe_manifest_hash_stable_for_same_inputs() -> None:
    a = UniverseManifest(
        universe_name="nifty50",
        as_of_date=date(2026, 1, 1),
        symbols=("RELIANCE", "TCS", "INFY"),
    )
    b = UniverseManifest(
        universe_name="nifty50",
        as_of_date=date(2026, 1, 1),
        symbols=("TCS", "INFY", "RELIANCE"),  # different order
    )
    assert a.universe_hash == b.universe_hash


def test_universe_manifest_hash_differs_for_different_as_of_dates() -> None:
    jan = UniverseManifest(
        universe_name="nifty50",
        as_of_date=date(2026, 1, 1),
        symbols=("RELIANCE",),
    )
    feb = UniverseManifest(
        universe_name="nifty50",
        as_of_date=date(2026, 2, 1),
        symbols=("RELIANCE",),
    )
    assert jan.universe_hash != feb.universe_hash


# ---------------------------------------------------------------------------
# FeatureManifest
# ---------------------------------------------------------------------------


def test_feature_manifest_rejects_empty_names() -> None:
    with pytest.raises(ValueError, match="feature_names"):
        FeatureManifest(
            feature_names=(),
            feature_version="v1",
            feature_ts_column="bar_ts",
            decision_ts_column="signal_ts",
        )


def test_feature_manifest_rejects_empty_version() -> None:
    with pytest.raises(ValueError, match="feature_version"):
        FeatureManifest(
            feature_names=("rsi_14",),
            feature_version="  ",
            feature_ts_column="bar_ts",
            decision_ts_column="signal_ts",
        )


def test_feature_manifest_rejects_identical_ts_columns() -> None:
    with pytest.raises(ValueError, match="must differ"):
        FeatureManifest(
            feature_names=("rsi_14",),
            feature_version="v1",
            feature_ts_column="ts",
            decision_ts_column="ts",
        )


def test_feature_manifest_hash_stable() -> None:
    a = FeatureManifest(
        feature_names=("rsi_14", "macd_signal"),
        feature_version="v1",
        feature_ts_column="bar_ts",
        decision_ts_column="signal_ts",
    )
    b = FeatureManifest(
        feature_names=("macd_signal", "rsi_14"),  # different order
        feature_version="v1",
        feature_ts_column="bar_ts",
        decision_ts_column="signal_ts",
    )
    assert a.manifest_hash == b.manifest_hash


# ---------------------------------------------------------------------------
# CostModelManifest
# ---------------------------------------------------------------------------


def test_cost_model_rejects_negative_commission() -> None:
    with pytest.raises(ValueError, match="non-negative"):
        CostModelManifest(commission_bps=-1.0, slippage_bps=2.0, impact_bps=1.0)


def test_cost_model_rejects_negative_slippage() -> None:
    with pytest.raises(ValueError, match="non-negative"):
        CostModelManifest(commission_bps=3.0, slippage_bps=-0.1, impact_bps=1.0)


def test_cost_model_total_round_trip_bps() -> None:
    m = CostModelManifest(commission_bps=3.0, slippage_bps=2.0, impact_bps=1.0)
    assert m.total_round_trip_bps == pytest.approx(12.0)  # 2 * (3+2+1)


def test_cost_model_hash_stable() -> None:
    a = CostModelManifest(commission_bps=3.0, slippage_bps=2.0, impact_bps=1.0)
    b = CostModelManifest(commission_bps=3.0, slippage_bps=2.0, impact_bps=1.0)
    assert a.manifest_hash == b.manifest_hash


# ---------------------------------------------------------------------------
# SimulationManifest
# ---------------------------------------------------------------------------


def test_simulation_manifest_rejects_zero_capital() -> None:
    with pytest.raises(ValueError, match="initial_capital"):
        SimulationManifest(
            engine_version="v1",
            initial_capital=0.0,
            position_sizing="fixed",
        )


def test_simulation_manifest_rejects_position_pct_out_of_range() -> None:
    with pytest.raises(ValueError, match="max_position_pct"):
        SimulationManifest(
            engine_version="v1",
            initial_capital=1_000_000.0,
            position_sizing="fixed",
            max_position_pct=1.5,
        )


def test_simulation_manifest_default_position_pct() -> None:
    m = SimulationManifest(
        engine_version="v1",
        initial_capital=1_000_000.0,
        position_sizing="equal_weight",
    )
    assert m.max_position_pct == pytest.approx(0.05)


# ---------------------------------------------------------------------------
# PromotionManifest
# ---------------------------------------------------------------------------


def _draft_promotion(**overrides) -> PromotionManifest:
    defaults = dict(
        experiment_id="exp-1",
        dataset_manifest_hash="d1" * 12,
        universe_manifest_hash="u1" * 12,
        feature_manifest_hash="f1" * 12,
        cost_manifest_hash="c1" * 12,
        result_hash="r1" * 12,
        code_revision="abc123",
    )
    defaults.update(overrides)
    return PromotionManifest(**defaults)


def test_promotion_manifest_draft_is_valid() -> None:
    m = _draft_promotion()
    assert m.approval is ApprovalState.DRAFT


def test_promotion_manifest_approved_requires_approved_by() -> None:
    with pytest.raises(ValueError, match="approved_by"):
        _draft_promotion(
            approval=ApprovalState.APPROVED,
            approved_at=datetime.now(UTC),
        )


def test_promotion_manifest_approved_requires_approved_at() -> None:
    with pytest.raises(ValueError, match="approved_at"):
        _draft_promotion(
            approval=ApprovalState.APPROVED,
            approved_by="alice",
        )


def test_promotion_manifest_approved_requires_code_revision() -> None:
    with pytest.raises(ValueError, match="code_revision"):
        _draft_promotion(
            approval=ApprovalState.APPROVED,
            approved_by="alice",
            approved_at=datetime.now(UTC),
            code_revision="unknown",
        )


def test_promotion_manifest_approved_full() -> None:
    m = _draft_promotion(
        approval=ApprovalState.APPROVED,
        approved_by="alice",
        approved_at=datetime.now(UTC),
        code_revision="deadbeef",
    )
    assert m.approval is ApprovalState.APPROVED
    assert m.approved_by == "alice"


def test_promotion_manifest_rejects_empty_experiment_id() -> None:
    with pytest.raises(ValueError, match="experiment_id"):
        _draft_promotion(experiment_id="  ")
