""".env.local: must contain TRADEX_RUNTIME_DIR."""

from __future__ import annotations

from pathlib import Path


def test_env_local_contains_runtime_dir() -> None:
    env_file = Path(".env.local")
    content = env_file.read_text()
    assert "TRADEX_RUNTIME_DIR" in content
