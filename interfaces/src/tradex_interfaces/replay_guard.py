from __future__ import annotations

import threading


class ReplayGuard:
    """Thread-safe ownership registry for active replay runs.

    At most one run id may be held process-wide. A second ``acquire`` while
    another id is active raises ``RuntimeError``.
    """

    def __init__(self) -> None:
        self._run_ids: set[str] = set()
        self._lock = threading.Lock()

    def acquire(self, run_id: str) -> None:
        if not isinstance(run_id, str) or not run_id.strip():
            raise ValueError("replay run id must be non-empty")
        with self._lock:
            if self._run_ids and run_id not in self._run_ids:
                raise RuntimeError("another replay run is already active")
            self._run_ids.add(run_id)

    def release(self, run_id: str) -> None:
        with self._lock:
            self._run_ids.discard(run_id)

    @property
    def active(self) -> bool:
        with self._lock:
            return bool(self._run_ids)


__all__ = ["ReplayGuard"]
