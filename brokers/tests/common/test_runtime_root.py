"""Single-runtime-root pin (cleanup overhaul, phase 2).

The old ``default_runtime_dir()`` fell back to ``Path.cwd() / "runtime"``,
so the *same* token/totp/instrument state forked into a different directory
per launch cwd (``trading/runtime/``, ``brokers/runtime/``). These tests pin
the deepened seam: one repo-anchored root, env override preserved.
"""

from __future__ import annotations

from pathlib import Path

from tradex_brokers.common.paths import (
    default_runtime_dir,
    default_token_state_path,
    default_totp_cooldown_path,
)

_REPO_ROOT = Path(__file__).resolve().parents[3]  # tests -> brokers -> repo


def test_runtime_dir_is_repo_anchored_not_cwd(tmp_path, monkeypatch) -> None:
    """chdir must not move the state root — the fork regression test."""
    monkeypatch.chdir(tmp_path)
    root = default_runtime_dir()
    assert root == _REPO_ROOT / "runtime"
    assert not (tmp_path / "runtime").exists(), "cwd fork reappeared"


def test_runtime_dir_env_override_wins(tmp_path, monkeypatch) -> None:
    monkeypatch.setenv("TRADEX_RUNTIME_DIR", str(tmp_path / "elsewhere"))
    root = default_runtime_dir()
    assert root == tmp_path / "elsewhere"
    assert root.is_dir()  # still created on demand


def test_state_paths_land_under_single_root(monkeypatch, tmp_path) -> None:
    monkeypatch.chdir(tmp_path)
    monkeypatch.delenv("TRADEX_RUNTIME_DIR", raising=False)
    root = default_runtime_dir()
    token = default_token_state_path("UPSTOX")
    cooldown = default_totp_cooldown_path("upstox")
    assert token == root / "upstox" / "token_state.json"
    assert cooldown == root / "upstox" / "totp_cooldown.json"
    assert _REPO_ROOT.joinpath("pyproject.toml").is_file(), "anchor must be repo root"