"""Root test bootstrap: pin every durable path to a temporary directory.

All workspace packages are installed editable by ``uv sync``, so no
``sys.path`` surgery is needed. ``TRADEX_RUNTIME_DIR`` is pinned **in
``pytest_configure``**, not in a session fixture: ``AppConfig`` and
``tradex_domain.paths`` resolve the variable per call, and a dozen test
modules freeze ``AppConfig()`` at *import* time, which is before any fixture
runs. Pinning in a fixture left those modules anchored to ``<repo>/runtime``
and wrote operator state into the repository. A test that wants the
repository-anchored default deletes the variable with ``monkeypatch.delenv``.

The same hook snapshots ``<repo>/runtime`` and ``pytest_sessionfinish`` fails
the session if anything under it appeared or changed, so the next regression
of this class cannot pass quietly.
"""

from __future__ import annotations

import os
import shutil
import tempfile
from pathlib import Path

import pytest

_REPO_ROOT = Path(__file__).resolve().parent
_REPO_RUNTIME_DIR = _REPO_ROOT / "runtime"

#: The directory pinned for this session, created in ``pytest_configure``.
_pinned_runtime_dir: str | None = None
#: ``TRADEX_RUNTIME_DIR`` as it was before this session pinned it.
_previous_runtime_dir: str | None = None
#: ``<repo>/runtime`` contents at configure time, as ``path -> (mtime_ns, size)``.
_runtime_snapshot: dict[str, tuple[int, int]] | None = None


def snapshot_runtime_dir(root: Path | str | None = None) -> dict[str, tuple[int, int]]:
    """Map every file under *root* to ``(mtime_ns, size)``.

    *root* defaults to the repository's ``runtime/`` directory. Keys are paths
    relative to *root*, so a snapshot survives a differing absolute prefix.
    Directories are not recorded: only writes matter here, and an existing
    directory being re-created is not one. Bytecode caches are not recorded
    either — ``runtime/`` also holds the ``tradex_runtime`` *source*, so the
    interpreter rewrites a ``.pyc`` there whenever a module is edited. That is
    a build artefact of tracked source, not operator state, and failing the
    suite for it would make every edit to that package a red run.
    """
    base = _REPO_RUNTIME_DIR if root is None else Path(root)
    if not base.is_dir():
        return {}
    snapshot: dict[str, tuple[int, int]] = {}
    for path in base.rglob("*"):
        if not path.is_file() or "__pycache__" in path.parts or path.suffix == ".pyc":
            continue
        try:
            stat = path.stat()
        except OSError:  # deleted between the walk and the stat
            continue
        snapshot[str(path.relative_to(base))] = (stat.st_mtime_ns, stat.st_size)
    return snapshot


def changed_runtime_paths(
    before: dict[str, tuple[int, int]],
    after: dict[str, tuple[int, int]],
) -> list[str]:
    """Paths that are new or modified between two :func:`snapshot_runtime_dir`."""
    return sorted(
        path for path, signature in after.items() if before.get(path) != signature
    )


def runtime_dir_violations() -> list[str]:
    """Paths under the repository's ``runtime/`` that this session created or changed.

    Empty means the suite is hermetic with respect to the repository; a
    non-empty list is what :func:`pytest_sessionfinish` turns into a failed
    session.
    """
    if _runtime_snapshot is None:
        return []
    return changed_runtime_paths(_runtime_snapshot, snapshot_runtime_dir())


def pytest_configure(config: pytest.Config) -> None:
    """Pin the runtime root before any test module is imported."""
    global _pinned_runtime_dir, _previous_runtime_dir, _runtime_snapshot
    _pinned_runtime_dir = tempfile.mkdtemp(prefix="tradex-runtime")
    _previous_runtime_dir = os.environ.get("TRADEX_RUNTIME_DIR")
    os.environ["TRADEX_RUNTIME_DIR"] = _pinned_runtime_dir
    _runtime_snapshot = snapshot_runtime_dir()


def pytest_unconfigure(config: pytest.Config) -> None:
    """Restore the previous runtime root and drop the pinned directory."""
    global _pinned_runtime_dir, _previous_runtime_dir, _runtime_snapshot
    if _previous_runtime_dir is None:
        os.environ.pop("TRADEX_RUNTIME_DIR", None)
    else:
        os.environ["TRADEX_RUNTIME_DIR"] = _previous_runtime_dir
    _previous_runtime_dir = None
    if _pinned_runtime_dir is not None:
        shutil.rmtree(_pinned_runtime_dir, ignore_errors=True)
        _pinned_runtime_dir = None
    _runtime_snapshot = None


def pytest_sessionfinish(session: pytest.Session, exitstatus: int) -> None:
    """Fail the session if the suite wrote into ``<repo>/runtime``."""
    offenders = runtime_dir_violations()
    if not offenders:
        return
    print("\nhermeticity violation: the suite wrote into the repository's runtime/")
    for path in offenders:
        print(f"  runtime/{path}")
    session.exitstatus = pytest.ExitCode.TESTS_FAILED


@pytest.fixture(scope="session")
def tradex_runtime_dir() -> str:
    """The runtime directory pinned for this session by ``pytest_configure``."""
    assert _pinned_runtime_dir is not None, "pytest_configure did not pin a runtime dir"
    return _pinned_runtime_dir
