"""`from_env` must refuse a typo and carry every live-control variable."""

from __future__ import annotations

from dataclasses import replace
from pathlib import Path

import pytest
from tradex_config.env import from_env, serve_config
from tradex_config.schema import PersistenceConfig
from tradex_domain import BrokerId
from tradex_domain.errors import SDKError
from tradex_execution.sqlite_store import SQLiteIdempotencyGuard

_ENV_KEYS = (
    "TRADEX_BROKER",
    "TRADEX_MODE",
    "TRADEX_LIVE_ENABLED",
    "TRADEX_LIVE_ORDERS_ENABLED",
    "TRADEX_PERSISTENCE_PATH",
    "TRADEX_STRATEGY_IDS",
)


@pytest.fixture(autouse=True)
def _clean_env(monkeypatch: pytest.MonkeyPatch) -> None:
    for key in _ENV_KEYS:
        monkeypatch.delenv(key, raising=False)


def test_an_unknown_broker_refuses_to_boot(monkeypatch) -> None:
    monkeypatch.setenv("TRADEX_BROKER", "dhann")
    with pytest.raises(SDKError, match="invalid TRADEX_BROKER"):
        from_env()


def test_strategy_ids_are_trimmed_and_deduplicated(monkeypatch) -> None:
    monkeypatch.setenv("TRADEX_STRATEGY_IDS", " alpha, beta ,alpha,, ")
    assert from_env().execution.strategy_ids == ("alpha", "beta")


def test_live_controls_are_read_from_the_environment(monkeypatch, tmp_path) -> None:
    monkeypatch.setenv("TRADEX_LIVE_ENABLED", "true")
    monkeypatch.setenv("TRADEX_LIVE_ORDERS_ENABLED", "true")
    monkeypatch.setenv("TRADEX_PERSISTENCE_PATH", str(tmp_path / "state.db"))

    cfg = from_env()

    assert cfg.live_enabled is True
    assert cfg.live_orders_enabled is True
    assert cfg.persistence.path == str(tmp_path / "state.db")


def test_serve_config_live_enables_the_gate_and_defaults_persistence(tmp_path) -> None:
    cfg = replace(from_env(), runtime_dir=str(tmp_path))
    assert not (tmp_path / "live").exists()

    live = serve_config(cfg, broker_id=BrokerId.DHAN, mode="live")

    assert live.broker_id is BrokerId.DHAN
    assert live.mode == "live"
    assert live.live_enabled is True
    assert live.persistence.path == str(tmp_path / "live" / "dhan.sqlite")
    # A fresh runtime directory has no ``live/`` child, and the config layer
    # must not create it as a side effect. The guard is the first store boot
    # opens, so it must create the parent itself — otherwise live boot dies on
    # ``unable to open database file`` before the writer lock's mkdir runs.
    assert live.persistence.path is not None
    assert not (tmp_path / "live").exists()
    guard = SQLiteIdempotencyGuard(live.persistence.path)
    try:
        assert Path(live.persistence.path).parent.is_dir()
    finally:
        guard.close()


def test_serve_config_live_keeps_an_explicit_persistence_path(tmp_path) -> None:
    cfg = replace(
        from_env(),
        runtime_dir=str(tmp_path),
        persistence=PersistenceConfig(path=str(tmp_path / "explicit.db")),
    )

    live = serve_config(cfg, broker_id=BrokerId.DHAN, mode="live")

    assert live.persistence.path == str(tmp_path / "explicit.db")


def test_serve_config_paper_changes_nothing_but_identity(tmp_path) -> None:
    cfg = replace(from_env(), runtime_dir=str(tmp_path))

    paper = serve_config(cfg, broker_id=BrokerId.PAPER, mode="paper")

    assert paper.broker_id is BrokerId.PAPER
    assert paper.mode == "paper"
    assert paper.live_enabled is False
    assert paper.persistence.path is None


def test_execution_config_normalises_strategy_ids() -> None:
    from tradex_config.schema import ExecutionConfig

    cfg = ExecutionConfig(strategy_ids=(" a ", "b", "a"))
    assert cfg.strategy_ids == ("a", "b")
