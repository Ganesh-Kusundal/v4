"""The suite must never write into the repository's ``runtime/`` directory.

``TRADEX_RUNTIME_DIR`` is read per call by both ``AppConfig`` and
``tradex_domain.paths``, so an unset variable means every session, guard and
lock in the suite resolves to ``<repo>/runtime`` and leaves databases there.
The root conftest pins it in ``pytest_configure``, before any test module is
imported, and fails the session if anything under ``<repo>/runtime`` changes.
This file proves the pin is in force and that the guard is not vacuous.
"""

from __future__ import annotations

import os
from pathlib import Path

from tradex_config.schema import AppConfig

_REPO_ROOT = Path(__file__).resolve().parent.parent


def test_the_suite_runtime_dir_is_a_temp_directory() -> None:
    runtime_dir = Path(os.environ["TRADEX_RUNTIME_DIR"]).resolve()
    assert runtime_dir.is_dir(), "pytest_configure must create the directory"
    assert runtime_dir.name.startswith("tradex-runtime"), runtime_dir
    assert runtime_dir != (_REPO_ROOT / "runtime").resolve()
    assert _REPO_ROOT not in runtime_dir.parents, (
        "a test durable path resolved inside the repository"
    )


def test_app_config_reads_the_pinned_runtime_dir() -> None:
    pinned = Path(os.environ["TRADEX_RUNTIME_DIR"]).resolve()
    assert Path(AppConfig().runtime_dir).resolve() == pinned


def test_the_guard_reports_a_clean_repository_runtime_dir() -> None:
    """The session guard's own snapshot API sees no writes into the repository.

    ``pytest_sessionfinish`` fails the session on any path this reports, so an
    empty answer here is the same evidence, asked earlier.
    """
    import conftest as root_conftest

    assert root_conftest.runtime_dir_violations() == [], (
        "the suite wrote into <repo>/runtime during this session"
    )


def test_the_guard_snapshot_detects_a_new_file(tmp_path: Path) -> None:
    """The guard is not vacuously empty: a new file under the root is reported."""
    import conftest as root_conftest

    root = tmp_path / "runtime"
    root.mkdir()
    before = root_conftest.snapshot_runtime_dir(root)
    assert root_conftest.changed_runtime_paths(before, before) == []

    (root / "state.db").write_bytes(b"x")

    after = root_conftest.snapshot_runtime_dir(root)
    assert root_conftest.changed_runtime_paths(before, after) == ["state.db"]
