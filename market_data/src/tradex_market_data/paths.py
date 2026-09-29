"""Datalake path anchoring — serve must find data/ regardless of process cwd.

Why this module exists: every consumer used to hand ParquetStorage the bare
relative literal ``"data/"``, which silently resolves against the *process*
cwd. When ``tradex serve`` is launched from anywhere but the repo root (on
2026-09-02: from ``trading/``), the root pointed at a nonexistent directory
and the chart API served ``source: "none"`` with zero bars — the frontend
rendered an empty shell and replay never opened.

The anchor resolves the repo root from ``tradex_domain.paths.REPO_ROOT``,
the single source of truth for repo-root anchoring.
"""

from __future__ import annotations

import os

from tradex_domain.paths import REPO_ROOT

#: Env override (``TRADEX_*``, matching the rest of the config surface). The
#: anchored default assumes the real lake sits at ``<repo>/data``; a fixture, a
#: shared mount or a CI job needs to point somewhere else without editing a
#: constant. Unset in normal runs, so serve still finds the real lake.
_ENV_OVERRIDE = os.environ.get("TRADEX_DATALAKE_ROOT")

#: Datalake root: ``$TRADEX_DATALAKE_ROOT`` when set, else <repo>/data. Absolute
#: either way, so a chdir mid-process can never move the store out from under a
#: running server.
DATALAKE_ROOT = _ENV_OVERRIDE or str(REPO_ROOT / "data")


def datalake_root() -> str:
    """Return the datalake root as a string path.

    ``ParquetStorage`` accepts str | Path; str keeps existing call sites and
    cache keys (``_get_store`` lru_cache is keyed on the base path) happy.
    """
    return DATALAKE_ROOT
