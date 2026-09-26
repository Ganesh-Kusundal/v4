"""Strategy and research artifact contract tests."""

from tradex_trading.strategy.artifacts import (
    ApprovalState,
    ExperimentManifest,
    InMemoryStrategyArtifactStore,
    StrategyArtifact,
)


def test_artifact_store_round_trips_versioned_artifact() -> None:
    store = InMemoryStrategyArtifactStore()
    artifact = StrategyArtifact("sma", "1.0.0", {"fast": 5})

    artifact_id = store.publish(artifact)

    assert store.load(artifact_id) == artifact


def test_strategy_artifact_id_is_stable_for_same_inputs() -> None:
    first = StrategyArtifact("sma", "1.0.0", {"fast": 5, "slow": 20})
    second = StrategyArtifact("sma", "1.0.0", {"slow": 20, "fast": 5})

    assert first.artifact_id == second.artifact_id
    assert first.approval is ApprovalState.DRAFT


def test_approved_artifact_requires_code_revision() -> None:
    try:
        StrategyArtifact("sma", "1.0.0", approval=ApprovalState.APPROVED)
    except ValueError as exc:
        assert "code revision" in str(exc)
    else:
        raise AssertionError("approved artifact without revision must fail")


def test_experiment_manifest_validates_search_budget() -> None:
    manifest = ExperimentManifest(
        experiment_id="exp-1",
        dataset_hash="d1",
        universe_hash="u1",
        feature_version="f1",
        label_definition="next-open",
        split_definition="walk-forward",
        parameters={"fast": 5},
        search_budget=10,
        random_seed=7,
        code_revision="abc123",
    )
    assert manifest.experiment_id == "exp-1"

    try:
        ExperimentManifest(
            experiment_id="bad",
            dataset_hash="d1",
            universe_hash="u1",
            feature_version="f1",
            label_definition="next-open",
            split_definition="walk-forward",
            parameters={},
            search_budget=0,
            random_seed=1,
            code_revision="abc",
        )
    except ValueError as exc:
        assert "search_budget" in str(exc)
    else:
        raise AssertionError("zero search budget must fail")
