"""Single source of truth for the TradeX runtime directory.

The runtime root holds token state, TOTP cooldowns, instrument caches and
workspace persistence. It used to be resolved independently by two layers:
``brokers/common/paths.py`` anchored to ``<repo>/runtime`` from ``__file__``,
while ``tradex_config`` fell back to the bare relative ``".tradex_v4"``. Both
read the same ``TRADEX_RUNTIME_DIR``, so with the variable unset the config
layer and the broker layer pointed at two *different* directories and forked
the same state. A bare relative default is cwd-relative, so the fork
multiplied by launch directory as well.

This module is the one resolver both layers now call. It lives in ``domain``
because that is the only package ``config`` may import, and it stays
stdlib-only so it can sit at the bottom of the dependency graph.
"""

from __future__ import annotations

import os
from pathlib import Path

#: Repo root, anchored from this file rather than the process cwd.
#: parents: tradex_domain -> src -> domain -> repo root.
REPO_ROOT: Path = Path(__file__).resolve().parents[3]

#: Default runtime root when ``TRADEX_RUNTIME_DIR`` is unset.
DEFAULT_RUNTIME_DIR: Path = REPO_ROOT / "runtime"

#: Environment variable that overrides the runtime root.
RUNTIME_DIR_ENV_VAR: str = "TRADEX_RUNTIME_DIR"


def default_runtime_dir(*, create: bool = True) -> Path:
    """Resolve the runtime directory for TradeX state and caches.

    Resolution order:

    1. ``$TRADEX_RUNTIME_DIR`` if set to a non-empty value.
    2. ``<repo>/runtime``, anchored from this file, so the answer is identical
       for every launch cwd.

    Why anchored: the previous fallback resolved relative to the process cwd
    (``.tradex_v4`` in config, ``runtime`` in brokers), which forked the same
    token/totp/instrument state into a different directory for every launch
    cwd — the class of bug ``datalake/paths.py`` fixed for the data lake on
    2026-09-02, and ``brokers/common/paths.py`` closed for broker state.

    Parameters
    ----------
    create:
        When true (default) the directory is created if missing, preserving
        the behaviour ``brokers.common.paths.default_runtime_dir`` always had.

    Returns
    -------
    Path
        The resolved runtime directory.
    """
    env_dir = os.environ.get(RUNTIME_DIR_ENV_VAR)
    path = Path(env_dir) if env_dir else DEFAULT_RUNTIME_DIR
    if create:
        path.mkdir(parents=True, exist_ok=True)
    return path


def default_token_state_path(broker_id: str) -> Path:
    """Return the default path for persisting a broker's token state.

    ``<runtime_dir>/<broker_id>/token_state.json``. Moved here from
    ``brokers/common/paths.py`` so the runtime resolver is single-owned.
    """
    path = default_runtime_dir() / broker_id.lower()
    path.mkdir(parents=True, exist_ok=True, mode=SECRET_DIR_MODE)
    return path / "token_state.json"


def default_totp_cooldown_path(broker: str) -> Path:
    """Return the default path for a broker's TOTP cooldown state.

    ``<runtime_dir>/<broker>/totp_cooldown.json``. Moved here from
    ``brokers/common/paths.py`` so the runtime resolver is single-owned.
    """
    path = default_runtime_dir() / broker.lower()
    path.mkdir(parents=True, exist_ok=True, mode=SECRET_DIR_MODE)
    return path / "totp_cooldown.json"


__all__ = [
    "DEFAULT_RUNTIME_DIR",
    "REPO_ROOT",
    "RUNTIME_DIR_ENV_VAR",
    "SECRET_DIR_MODE",
    "default_runtime_dir",
    "default_token_state_path",
    "default_totp_cooldown_path",
]


#: Mode for freshly created per-broker state directories, which hold
#: credentials (``token_state.json``) and cooldown state. Owner-only instead
#: of the umask's 0755. ``exist_ok=True`` deliberately leaves pre-existing
#: directories alone: this never chmods a directory the user (or a prior run)
#: already has. The file mode set by the atomic writer is the actual control;
#: the directory is defense in depth for freshly created paths.
SECRET_DIR_MODE = 0o700
