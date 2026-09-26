"""Single-writer guard for live trading (closes risk R2).

Rate limiters are per-process, so two live writer processes on the same
account can jointly breach provider limits. This lockfile makes the
single-writer rule *enforced* rather than conventional:

- ``acquire()`` creates ``<path>`` exclusively (O_EXCL), takes an
  exclusive ``flock`` on it and writes the pid into that same inode.
- If the file exists and the pid is alive → fail-closed error naming holder.
- If the file exists but the pid is dead (crash) → stale, auto-cleared.

Atomicity (TOCTOU)
------------------
The first version decided staleness, then ``unlink()``-ed the file, then
re-created it with O_EXCL. Those steps are three separate syscalls, so two
contenders could interleave: A reads a stale pid, B creates the lock and
writes its own pid, A then unlinks *B's* fresh claim and creates its own.
Both ``acquire()`` calls returned and both processes traded the account.

The claim is now the exclusive ``flock`` on the lock file's inode, and the
takeover rewrites the pid *into the same inode it just locked*:

- ``flock`` cannot be taken → a live holder owns this very inode, the
  claim is intact, and the contender refuses without touching the file.
- ``flock`` is free → the kernel guarantees no live holder owns this
  inode (the kernel drops ``flock`` when a process dies), so the
  remaining question is only whether the recorded pid is dead. A live
  pid is still refused (locks written by an older build never flocked);
  a dead one is taken over in place.

A contender that loses the race never writes, truncates or unlinks, so it
can no longer erase another process's claim, and the pid on disk is always
the current holder's.
"""

from __future__ import annotations

import errno
import fcntl
import logging
import os
from pathlib import Path

log = logging.getLogger(__name__)

# Errors meaning "someone else already holds the flock".
_FLOCK_BUSY = frozenset(
    code
    for code in (errno.EACCES, errno.EAGAIN, getattr(errno, "EWOULDBLOCK", errno.EAGAIN))
)


class WriterLockHeldError(RuntimeError):
    """Another live process holds the single-writer lock."""


class SingleWriterLock:
    """Exclusive, crash-reclaimable claim on a lock file.

    Holding the lock means holding an exclusive ``flock`` on the pid file's
    inode for the whole lifetime of the claim. The kernel releases that
    flock when the process dies, so an unclean shutdown leaves a reclaimable
    remnant rather than a lock nobody can take.
    """

    def __init__(self, path: Path | str) -> None:
        self._path = Path(path)
        self._held = False
        self._fd: int | None = None

    def acquire(self) -> None:
        self._path.parent.mkdir(parents=True, exist_ok=True)
        while True:
            # ``_takeover`` returns ``int | None`` (None == holder released
            # between our O_EXCL and its open), so ``fd`` is optional until the
            # ``is None`` guard below narrows it to ``int`` for the rest of the
            # loop body.
            fd: int | None
            try:
                fd = os.open(self._path, os.O_CREAT | os.O_EXCL | os.O_RDWR)
            except FileExistsError:
                # A lock file exists: another holder, or a crashed
                # holder's remnant. Deciding whether to take it over is the
                # race that used to be lost, so it happens under an
                # exclusive flock on that exact inode.
                fd = self._takeover()
                if fd is None:
                    # Holder released between our O_EXCL and its open;
                    # retry the create.
                    continue
            try:
                self._write_pid(fd)
            except BaseException:
                self._close_fd(fd)
                raise
            self._fd = fd
            self._held = True
            return

    def release(self) -> None:
        if not self._held:
            return
        fd, self._fd = self._fd, None
        self._held = False
        try:
            # Unlink first, then drop the flock: a contender that opened
            # the old inode before the unlink stays blocked until the fd
            # is closed, and a contender that opens the path afterwards
            # gets a brand-new inode it can claim cleanly.
            self._path.unlink(missing_ok=True)
        finally:
            # Closing the fd drops the flock implicitly. Unlock explicitly
            # first, so the kernel is never asked to keep a lock on an fd
            # number that a concurrent acquirer may already have recycled.
            if fd is not None:
                self._unflock_then_close(fd)

    def _takeover(self) -> int | None:
        """Claim the existing lock file atomically, or refuse.

        Returns the fd of the claimed inode (its pid has *not* been written
        yet — the caller does that while still holding the flock), ``None``
        if the file vanished and the caller should retry, or raises
        ``WriterLockHeldError``.
        """
        try:
            fd = os.open(self._path, os.O_RDWR)
        except FileNotFoundError:
            return None
        except PermissionError as exc:
            # Cannot read the claim, so cannot rule out a live holder.
            # Fail closed: two live writers on one account is the risk
            # this lock exists to prevent.
            raise WriterLockHeldError(
                f"cannot open {self._path} to verify the single-writer "
                f"claim ({exc}); refusing to start a second live writer"
            ) from None

        claimed = False
        try:
            if not self._try_flock(fd):
                # Someone holds the claim on this inode right now.
                holder = self._read_pid()
                raise WriterLockHeldError(
                    f"another live writer holds {self._path} (pid {holder}); "
                    "only one live trading process may run per account"
                ) from None

            # We hold the claim on this inode; every other contender now
            # sees it locked and refuses. The recorded pid is read through
            # our own fd, so it is the pid of *this* inode even if the path
            # was replaced a moment ago.
            holder = self._read_pid_from_fd(fd)
            if holder is not None and self._pid_alive(holder):
                # Lock written by an older build that never flocked, or a
                # pid we cannot prove is gone. Refuse, as before.
                raise WriterLockHeldError(
                    f"another live writer holds {self._path} (pid {holder}); "
                    "only one live trading process may run per account"
                ) from None

            log.warning(
                "stale writer lock %s (pid %s gone); taking over",
                self._path, holder,
            )
            claimed = True
            return fd
        finally:
            if not claimed:
                self._close_fd(fd)

    @staticmethod
    def _try_flock(fd: int) -> bool:
        """Take the exclusive claim. False means someone else holds it."""
        try:
            fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except OSError as exc:
            if exc.errno in _FLOCK_BUSY:
                return False
            raise
        return True

    def _write_pid(self, fd: int) -> None:
        """Publish this process's pid into the inode we have claimed."""
        os.lseek(fd, 0, os.SEEK_SET)
        os.ftruncate(fd, 0)
        os.write(fd, str(os.getpid()).encode())
        os.fsync(fd)

    def _read_pid(self) -> int | None:
        try:
            return int(self._path.read_text().strip())
        except (OSError, ValueError):
            return None

    def _read_pid_from_fd(self, fd: int) -> int | None:
        """Read the recorded pid through an fd we already hold the flock on."""
        try:
            os.lseek(fd, 0, os.SEEK_SET)
            raw = os.read(fd, 128)
        except OSError:
            return None
        try:
            return int(raw.decode("utf-8", "replace").strip())
        except ValueError:
            return None

    @staticmethod
    def _unflock_then_close(fd: int) -> None:
        try:
            fcntl.flock(fd, fcntl.LOCK_UN)
        except OSError:
            pass
        SingleWriterLock._close_fd(fd)

    @staticmethod
    def _close_fd(fd: int) -> None:
        try:
            os.close(fd)
        except OSError:
            pass

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
