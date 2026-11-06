"""Runtime secret permissions sweep (config-level).

Covers ``tradex_config.permissions.assert_runtime_secret_permissions``:
scans the broker runtime state tree + repo-root ``.env.local`` and refuses
any group/world-readable file.
"""

from __future__ import annotations

import stat
from pathlib import Path

import pytest

from tradex_config.permissions import (
    InsecureSecretFileError,
    assert_runtime_secret_permissions,
)


def _make_tree(base: Path, *, bad: str | None = None) -> None:
    """Build a runtime tree with all secret files at 0600 except ``bad``."""
    (base / "dhan").mkdir(parents=True, exist_ok=True)
    (base / "upstox").mkdir(parents=True, exist_ok=True)

    files = [
        base / "dhan" / "token_state.json",
        base / "upstox" / "token_state.json",
        base / "dhan.idempotency.db",
        base / "paper.idempotency.db",
        base / "workspace.sqlite",
    ]
    for p in files:
        p.write_text("{}")
        p.chmod(0o644 if bad == str(p) else 0o600)


def _mode(p: Path) -> int:
    return stat.S_IMODE(p.stat().st_mode)


def test_all_private_returns(tmp_path: Path) -> None:
    base = tmp_path / "runtime"
    base.mkdir()
    _make_tree(base, bad=None)
    # all 0600 -> must not raise
    assert_runtime_secret_permissions(base)


def test_one_permissive_file_raises(tmp_path: Path) -> None:
    base = tmp_path / "runtime"
    base.mkdir()
    bad = base / "dhan" / "token_state.json"
    _make_tree(base, bad=str(bad))
    # ensure the bad file is actually 0644
    assert _mode(bad) == 0o644
    with pytest.raises(InsecureSecretFileError):
        assert_runtime_secret_permissions(base)


def test_missing_env_local_tolerated(tmp_path: Path) -> None:
    base = tmp_path / "runtime"
    base.mkdir()
    _make_tree(base, bad=None)
    # no .env.local anywhere -> must not raise
    assert_runtime_secret_permissions(base)


def test_permissive_env_local_raises(tmp_path: Path) -> None:
    base = tmp_path / "runtime"
    base.mkdir()
    _make_tree(base, bad=None)
    env = base.parent / ".env.local"
    env.write_text("SECRET=1\n")
    env.chmod(0o644)
    try:
        with pytest.raises(InsecureSecretFileError):
            assert_runtime_secret_permissions(base)
    finally:
        env.unlink(missing_ok=True)
