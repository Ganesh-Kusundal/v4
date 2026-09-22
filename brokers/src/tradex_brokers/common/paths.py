"""Runtime filesystem paths for broker state and caches.

Centralises the default directories used for token state, instrument caches,
and other runtime artefacts so that all broker adapters use a consistent,
cwd-independent layout: one runtime root, never per-launch-cwd forks.
"""

from __future__ import annotations

import os
from pathlib import Path


def default_runtime_dir() -> Path:
    """Return the default runtime directory for TradeX broker state.

    Resolution order:
    1. ``$TRADEX_RUNTIME_DIR`` environment variable if set.
    2. ``<repo>/runtime`` anchored from this file — cwd-independent, the
       exact pattern ``tradex_trading.datalake.paths`` uses for the lake.

    Why anchored: the old fallback (``Path.cwd() / "runtime"``) forked the
    *same* token/totp/instrument state into a different directory for every
    launch cwd — ``trading/runtime/`` when a process started from
    ``trading/``, ``brokers/runtime/`` likewise — so two writers could each
    believe they owned the session (the same cwd bug that created the shadow
    ``trading/data/`` lake, fixed for the lake by ``datalake/paths.py`` on
    2026-09-02; this closes it for runtime state).

    The directory is created if it does not exist.
    """
    env_dir = os.environ.get("TRADEX_RUNTIME_DIR")
    if env_dir:
        path = Path(env_dir)
    else:
        # parents: common -> tradex_brokers -> src -> brokers -> repo root
        path = Path(__file__).resolve().parents[4] / "runtime"
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
