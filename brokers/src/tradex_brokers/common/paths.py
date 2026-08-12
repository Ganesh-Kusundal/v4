"""Runtime filesystem paths for broker state and caches.

Centralises the default directories used for token state, instrument caches,
and other runtime artefacts so that all broker adapters use a consistent
layout.
"""

from __future__ import annotations

import os
from pathlib import Path


def default_runtime_dir() -> Path:
    """Return the default runtime directory for TradeX broker state.

    Resolution order:
    1. ``$TRADEX_RUNTIME_DIR`` environment variable if set.
    2. ``./runtime`` relative to the current working directory.

    The directory is created if it does not exist.
    """
    env_dir = os.environ.get("TRADEX_RUNTIME_DIR")
    if env_dir:
        path = Path(env_dir)
    else:
        path = Path.cwd() / "runtime"
    path.mkdir(parents=True, exist_ok=True)
    return path


def default_token_state_path(broker_id: str) -> Path:
    """Return the default path for persisting a broker's token state.

    Parameters
    ----------
    broker_id:
        Broker identifier (e.g. ``"DHAN"``, ``"UPSTOX"``, ``"PAPER"``).

    Returns
    -------
    Path
        ``<runtime_dir>/<broker_id>/token_state.json``
    """
    path = default_runtime_dir() / broker_id.lower()
    path.mkdir(parents=True, exist_ok=True)
    return path / "token_state.json"


def default_totp_cooldown_path(broker: str) -> Path:
    """Return the default path for a broker's TOTP cooldown state.

    Parameters
    ----------
    broker:
        Broker identifier (e.g. ``"dhan"``, ``"upstox"``).

    Returns
    -------
    Path
        ``<runtime_dir>/<broker>/totp_cooldown.json``
    """
    path = default_runtime_dir() / broker.lower()
    path.mkdir(parents=True, exist_ok=True)
    return path / "totp_cooldown.json"


__all__ = [
    "default_totp_cooldown_path",
    "default_runtime_dir",
    "default_token_state_path",
]
