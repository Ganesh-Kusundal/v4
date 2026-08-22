"""Single-writer guard for live trading (closes risk R2).

Rate limiters are per-process, so two live writer processes on the same
account can jointly breach provider limits. This lockfile makes the
single-writer rule *enforced* rather than conventional:

- ``acquire()`` creates ``<path>`` exclusively (O_EXCL) and writes the pid.
- If the file exists and its pid is alive → fail-closed error naming holder.
- If the file exists but the pid is dead (crash) → stale, auto-cleared.
"""

from __future__ import annotations

import logging
import os
from pathlib import Path

log = logging.getLogger(__name__)


class WriterLockHeldError(RuntimeError):
    """Another live process holds the single-writer lock."""


class SingleWriterLock:
    def __init__(self, path: Path | str) -> None:
        self._path = Path(path)
        self._held = False

    def acquire(self) -> None:
        self._path.parent.mkdir(parents=True, exist_ok=True)
        while True:
            try:
                fd = os.open(self._path, os.O_CREAT | os.O_EXCL | os.O_WRONLY)
            except FileExistsError:
                holder = self._read_pid()
                if holder is not None and self._pid_alive(holder):
                    raise WriterLockHeldError(
                        f"another live writer holds {self._path} (pid {holder}); "
                        "only one live trading process may run per account"
                    ) from None
                log.warning("stale writer lock %s (pid %s gone); clearing", self._path, holder)
                self._path.unlink(missing_ok=True)
                continue
            with os.fdopen(fd, "w") as fh:
                fh.write(str(os.getpid()))
            self._held = True
            return

    def release(self) -> None:
        if self._held:
            self._path.unlink(missing_ok=True)
            self._held = False

    def _read_pid(self) -> int | None:
        try:
            return int(self._path.read_text().strip())
        except (OSError, ValueError):
            return None

    @staticmethod
    def _pid_alive(pid: int) -> bool:
        if pid <= 0:
            return False
        try:
            os.kill(pid, 0)  # signal 0 = existence check
            return True
        except ProcessLookupError:
            return False
        except PermissionError:  # alive but not ours
            return True