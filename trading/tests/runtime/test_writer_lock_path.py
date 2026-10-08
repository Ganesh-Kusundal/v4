"""SingleWriterLock path: must resolve TRADEX_RUNTIME_DIR.

The lock file path must be absolute and resolved from TRADEX_RUNTIME_DIR,
not cwd-relative. A cwd-relative path means two processes started from
different directories create different lock files, defeating the
single-writer guarantee.
"""

from __future__ import annotations

from pathlib import Path

from tradex_config.schema import default_runtime_dir_str
from tradex_runtime.writer_lock import SingleWriterLock


def test_lock_path_is_absolute() -> None:
    """The lock path must be absolute regardless of cwd."""
    lock = SingleWriterLock(Path("runtime/live/test.writer.lock"))
    assert lock.path.is_absolute()


def test_lock_path_resolves_runtime_dir() -> None:
    """The lock path must be anchored to the resolved runtime dir."""
    lock = SingleWriterLock(Path("runtime/live/test.writer.lock"))
    expected_root = Path(default_runtime_dir_str())
    assert str(lock.path).startswith(str(expected_root))


def test_lock_path_uses_default_runtime_dir() -> None:
    """When TRADEX_RUNTIME_DIR is unset, the path uses the repo-anchored default."""
    lock = SingleWriterLock(Path("runtime/live/test.writer.lock"))
    default_root = Path(default_runtime_dir_str())
    assert default_root.is_absolute()
    assert str(lock.path).startswith(str(default_root))
