"""Root test bootstrap: pin every durable path to a temporary directory.

All workspace packages are installed editable by ``uv sync``, so no
``sys.path`` surgery is needed. ``TRADEX_RUNTIME_DIR`` is pinned once per
session because ``AppConfig`` and ``tradex_domain.paths`` resolve it per call;
a test that wants the repository-anchored default deletes the variable with
``monkeypatch.delenv``.
"""

from __future__ import annotations

import os
from collections.abc import Iterator

import pytest


@pytest.fixture(scope="session", autouse=True)
def tradex_runtime_dir(tmp_path_factory: pytest.TempPathFactory) -> Iterator[str]:
    runtime_dir = tmp_path_factory.mktemp("tradex-runtime")
    previous = os.environ.get("TRADEX_RUNTIME_DIR")
    os.environ["TRADEX_RUNTIME_DIR"] = str(runtime_dir)
    try:
        yield str(runtime_dir)
    finally:
        if previous is None:
            os.environ.pop("TRADEX_RUNTIME_DIR", None)
        else:
            os.environ["TRADEX_RUNTIME_DIR"] = previous
