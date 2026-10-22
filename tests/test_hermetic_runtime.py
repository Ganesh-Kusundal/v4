"""The suite must never write into the repository's ``runtime/`` directory.

``TRADEX_RUNTIME_DIR`` is read per call by both ``AppConfig`` and
``tradex_domain.paths``, so an unset variable means every session, guard and
lock in the suite resolves to ``<repo>/runtime`` and leaves databases there.
The root conftest pins it to a pytest temp directory for the whole session;
this file proves the pin is in force and that the config layer sees it.
"""

from __future__ import annotations

import os
from pathlib import Path

from tradex_config.schema import AppConfig

_REPO_ROOT = Path(__file__).resolve().parent.parent


def test_the_suite_runtime_dir_is_a_temp_directory() -> None:
    runtime_dir = Path(os.environ["TRADEX_RUNTIME_DIR"]).resolve()
    assert runtime_dir.is_dir(), "the session fixture must create the directory"
    assert runtime_dir.name.startswith("tradex-runtime"), runtime_dir
    assert runtime_dir != (_REPO_ROOT / "runtime").resolve()
    assert _REPO_ROOT not in runtime_dir.parents, (
        "a test durable path resolved inside the repository"
    )


def test_app_config_reads_the_pinned_runtime_dir() -> None:
    pinned = Path(os.environ["TRADEX_RUNTIME_DIR"]).resolve()
    assert Path(AppConfig().runtime_dir).resolve() == pinned
