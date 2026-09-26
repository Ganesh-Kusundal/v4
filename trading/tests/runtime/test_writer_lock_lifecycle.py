"""M7 — global ``_ACTIVE_WRITER_LOCK`` clobbers on multiple live boots.

The principal-architect review (M7) found that
``runtime.startup._ACTIVE_WRITER_LOCK`` is a module-global that the
first ``boot()`` writes and the second ``boot()`` overwrites without
releasing the first. Two live sessions on the same account then
trample each other's writer lock.

The fix: the live boot path stores the writer lock on the
``RuntimeContext`` (a local) instead of a module global. The
``RuntimeContext.close()`` releases the lock it acquired, regardless
of how many other live contexts exist. ``_ACTIVE_WRITER_LOCK`` stays
in the module as a process-wide fallback (e.g. for atexit), but
is no longer the authoritative holder.

Tests pin:
  - Two sequential live boots: each acquires its own lock; the
    second's close() does NOT release the first's lock (each
    context owns its own).
  - After two live boots, both locks are still held (not leaked)
    until each RuntimeContext.close() runs.

Race suite (same file, since it is the only test module allowed to
change for the TOCTOU fix):
  - ``acquire()`` stale-lock takeover must be atomic. The pre-fix
    code read the holder pid, judged it dead, then ``unlink()``-ed
    the file — a non-atomic claim. Two processes could interleave so
    that A unlinked the lock B had *just* created, and both returned
    from ``acquire()`` believing they owned the account.
"""

from __future__ import annotations

import fcntl
import json
import os
import subprocess
import sys
import threading
import time
from pathlib import Path
from unittest.mock import MagicMock

import pytest

import tradex_runtime.startup as startup_mod
from tradex_domain import BrokerId
from tradex_runtime.startup import RuntimeContext
from tradex_runtime.writer_lock import SingleWriterLock, WriterLockHeldError

from tradex_trading.config.schema import AppConfig, PersistenceConfig


# --------------------------------------------------------------------------
# Helpers shared by the stale-lock takeover race tests
# --------------------------------------------------------------------------

# The races need a lock file that *every* contender is entitled to take
# over, i.e. one naming a pid that really died. That is the state the
# pre-fix code mishandled: it judged the lock stale, unlinked the file
# and only then re-created it, three separate steps a second contender
# could slip between.

def _live_writer_lock(path: Path, pid: int) -> None:
    """Write ``path`` holding ``pid`` AND an open flock lease (live holder)."""
    fd = os.open(path, os.O_RDWR | os.O_CREAT, 0o644)
    try:
        fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
        with os.fdopen(os.dup(fd), "w") as fh:
            fh.write(str(pid))
            fh.flush()
            os.fsync(fh.fileno())
    finally:
        os.close(fd)


def _reaped_pid() -> int:
    """A pid that is guaranteed dead: fork a child, reap it, return its pid.

    ``os.fork`` rather than ``multiprocessing`` so no interpreter state is
    inherited; the child exits immediately and ``os.waitpid`` reaps it, so
    ``os.kill(pid, 0)`` raises ``ProcessLookupError`` for the rest of the
    test. (A pid far above the live range would risk colliding with a real
    process spawned in the meantime, which would flip the staleness verdict
    and mask the defect under test.)
    """
    pid = os.fork()
    if pid == 0:  # child
        os._exit(0)
    os.waitpid(pid, 0)
    return pid


def _dead_writer_lock(path: Path) -> int:
    """Write a lock file naming a reaped (dead) pid, and return that pid.

    This is the state the pre-fix code mishandled: it judged the lock
    stale, unlinked the file, and only then re-created it, so a second
    contender could have its fresh claim unlinked in between.
    """
    pid = _reaped_pid()
    path.write_text(str(pid))
    return pid


def _pid_exists(pid: int) -> bool:
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        return True
    return True


# --------------------------------------------------------------------------
# Real-OS-process contenders
#
# ``multiprocessing`` spawn re-imports this module by a pytest import-mode
# dependent name, which does not resolve under ``--import-mode=importlib``.
# Separate interpreters started with ``subprocess`` (the pattern already
# used in trading/tests/interface) are the more faithful race anyway.
# --------------------------------------------------------------------------


_CONTENDER = '''
import json, os, sys, time

lock_path, start_at, hold_s, retries, release_when_done, srcs = json.loads(sys.argv[1])
sys.path[:0] = srcs
from tradex_runtime.writer_lock import SingleWriterLock, WriterLockHeldError


def run():
    lock = SingleWriterLock(lock_path)
    # Spin until the shared start instant so the contenders really collide.
    while time.time() < start_at:
        time.sleep(0.001)

    for _ in range(retries):
        try:
            lock.acquire()
        except WriterLockHeldError:
            if retries == 1:
                print(json.dumps({"pid": os.getpid(), "outcome": "refused"}))
                return
            time.sleep(0.02)
            continue
        except BaseException as exc:
            print(json.dumps({"pid": os.getpid(), "outcome": "error",
                              "detail": repr(exc)}))
            return
        # Report the pid currently on disk *while holding*, so a rival that
        # erased this claim is caught here rather than masked by the release.
        try:
            on_disk = open(lock_path).read().strip()
        except OSError:
            on_disk = None
        print(json.dumps({"pid": os.getpid(), "outcome": "acquired",
                          "on_disk": on_disk}), flush=True)
        time.sleep(hold_s)
        if release_when_done:
            lock.release()
        return
    print(json.dumps({"pid": os.getpid(), "outcome": "refused"}))


run()
'''


def _runtime_srcs() -> list[str]:
    """The v4 src dirs, so a spawned interpreter can import the packages.

    Mirrors the root conftest, which puts these on ``sys.path`` for the
    in-process test run.
    """
    import tradex_runtime

    v4 = Path(tradex_runtime.__file__).resolve().parents[2]
    return [str(v4 / "runtime" / "src"), str(v4 / "domain" / "src")]


def _start_contenders(
    lock_path: Path,
    count: int,
    *,
    hold_s: float = 0.2,
    retries: int = 1,
    release_when_done: bool = False,
    start_delay: float = 1.0,
) -> list[subprocess.Popen]:
    """Launch ``count`` interpreters that all race for ``lock_path``.

    ``retries`` > 1 lets a contender keep trying after a refusal, which is
    how a pre-fix implementation still gets its chance to double-acquire
    (it refuses latecomers once the winner has replaced the dead pid).
    """
    srcs = _runtime_srcs()
    program = _CONTENDER
    start_at = time.time() + start_delay
    env = dict(os.environ)
    env["PYTHONPATH"] = os.pathsep.join(srcs + [env.get("PYTHONPATH", "")])

    procs = []
    for _ in range(count):
        payload = json.dumps(
            [str(lock_path), start_at, hold_s, retries, release_when_done, srcs]
        )
        procs.append(
            subprocess.Popen(
                [sys.executable, "-c", program, payload],
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                text=True,
                env=env,
            )
        )
    return procs


def _collect(procs: list[subprocess.Popen], timeout: float = 60.0) -> list[dict]:
    """Wait for every contender and parse its single JSON result line."""
    results = []
    for proc in procs:
        out, err = proc.communicate(timeout=timeout)
        assert proc.returncode == 0, (
            f"contender exited {proc.returncode}\nstdout={out!r}\nstderr={err!r}"
        )
        lines = [line for line in out.splitlines() if line.strip()]
        assert len(lines) == 1, f"expected one result line, got {lines!r} (stderr={err!r})"
        results.append(json.loads(lines[-1]))
    return results


# --------------------------------------------------------------------------
# Stale-lock takeover must be atomic
# --------------------------------------------------------------------------


def test_concurrent_stale_takeover_has_exactly_one_winner(tmp_path) -> None:
    """RED (pre-fix): a stale lock must be taken over by exactly ONE caller.

    Eight threads are released at once against a lock file naming a pid
    that really died, so all of them are entitled to reclaim it. The fix
    makes the staleness verdict and the takeover a single atomic step (an
    exclusive flock on the inode), so the first thread to reach it claims
    the file and every other one is refused.

    Pre-fix, the verdict ("holder is dead") and the ``unlink()`` were two
    separate syscalls, so threads that read the dead pid before the file
    went away could each unlink and re-create it, and more than one of them
    returned from ``acquire()`` believing it owned the account.
    """
    lock_path = tmp_path / "dhan.writer.lock"
    contenders = 12
    _dead_writer_lock(lock_path)  # crashed writer: the pid is reaped

    start = threading.Barrier(contenders)
    settled = threading.Barrier(contenders)  # everyone has finished trying
    acquired: list[int] = []          # thread indices that got the lock
    refused: list[int] = []           # thread indices that got refused
    on_disk: list[str] = []           # pid the winner saw, while it held
    errors: list[BaseException] = []
    errors_lock = threading.Lock()
    refused_lock = threading.Lock()
    writes_lock = threading.Lock()

    def _contend(index: int) -> None:
        lock = SingleWriterLock(lock_path)
        try:
            start.wait()
            try:
                lock.acquire()
            except WriterLockHeldError:
                with refused_lock:
                    refused.append(index)
                return
            with writes_lock:
                acquired.append(index)
                # Read it back while this thread still owns the lock, so the
                # assertion cannot be satisfied by a later re-acquire.
                on_disk.append(lock_path.read_text().strip())
            # Keep holding until every contender has finished trying, so a
            # second winner cannot slip in behind this one's release and be
            # counted as a double-acquire when it is really a later re-acquire.
        except BaseException as exc:  # noqa: BLE001 - surfaced in the assert
            with errors_lock:
                errors.append(exc)
        finally:
            # Every contender crosses this barrier exactly once, so the
            # winner is still holding the lock when the last one arrives.
            settled.wait()
            # release() is a no-op for a lock we failed to acquire.
            lock.release()

    threads = [threading.Thread(target=_contend, args=(i,)) for i in range(contenders)]
    for t in threads:
        t.start()
    for t in threads:
        t.join(timeout=60)
    for t in threads:
        assert not t.is_alive(), "contender thread hung"

    assert not errors, f"unexpected errors during race: {errors!r}"
    assert len(acquired) == 1, (
        "TOCTOU: single-writer lock handed to "
        f"{len(acquired)} of {contenders} racing processes {sorted(acquired)}; "
        "exactly one may acquire"
    )
    assert len(refused) == contenders - 1, (
        f"expected {contenders - 1} refusals, got {len(refused)}"
    )

    # The on-disk pid, read by the winner while it owned the lock, must be
    # this process's pid: no loser rewrote the claim underneath it. (The
    # threads share a pid, so this checks the claim was not overwritten
    # rather than identifying the winner by pid.)
    assert on_disk == [str(os.getpid())], (
        f"on-disk pid was {on_disk!r}, expected ['{os.getpid()}']"
    )


def test_concurrent_stale_takeover_across_processes_single_winner(tmp_path) -> None:
    """RED (pre-fix): same invariant, but with real OS processes.

    Separate interpreters, so their pids differ and the surviving on-disk
    pid can be checked against the process that actually won. Each contender
    retries after a refusal, because a pre-fix implementation refuses
    latecomers once the winner has replaced the dead pid with its own live
    one, and the defect is precisely that a contender arriving in the gap
    before that replacement got through.
    """
    lock_path = tmp_path / "upstox.writer.lock"
    contenders = 8
    _dead_writer_lock(lock_path)  # crashed writer: the pid is reaped

    procs = _start_contenders(
        lock_path, contenders, hold_s=2.0, retries=1, start_delay=1.5
    )
    outcomes = _collect(procs)
    errors = [o for o in outcomes if o["outcome"] == "error"]
    assert not errors, f"unexpected contender errors: {errors!r}"

    acquired = [o for o in outcomes if o["outcome"] == "acquired"]
    refused = [o for o in outcomes if o["outcome"] == "refused"]
    assert len(acquired) == 1, (
        f"TOCTOU: single-writer lock handed to {len(acquired)} of {contenders} "
        f"racing processes {[o['pid'] for o in acquired]}; exactly one may "
        f"acquire. refused={[o['pid'] for o in refused]}"
    )
    assert len(refused) == contenders - 1, (
        f"expected {contenders - 1} refusals, got {len(refused)}"
    )

    # The pid on disk while the winner held the lock must be the winner's:
    # nobody else's claim was left behind.
    winner = acquired[0]
    assert winner["on_disk"] == str(winner["pid"]), (
        f"on-disk pid {winner['on_disk']!r} is not the winning process "
        f"{winner['pid']!r}"
    )


def test_on_disk_pid_matches_winner_after_takeover(tmp_path) -> None:
    """Take over a crashed writer's lock: only the winner's pid lands on disk.

    Two real processes both find a dead-pid remnant reclaimable. Exactly one
    takes over, and the pid left in the file must be that winner's — the
    invariant the pre-fix ``unlink()``-then-create sequence violated,
    because a loser could unlink the winner's freshly written claim and
    write its own over it.
    """
    lock_path = tmp_path / "dhan.writer.lock"
    _dead_writer_lock(lock_path)  # crashed writer: the pid is reaped

    # The winner does not release, so the file the test inspects is the
    # winner's own claim and not an artefact of a later release.
    procs = _start_contenders(
        lock_path, 2, hold_s=2.0, retries=1, release_when_done=False, start_delay=1.5
    )
    results = _collect(procs)
    assert not [r for r in results if r["outcome"] == "error"], results
    winners = [r for r in results if r["outcome"] == "acquired"]
    assert len(winners) == 1, f"expected one winner, got {results!r}"
    assert lock_path.read_text().strip() == str(winners[0]["pid"]), (
        "on-disk pid is not the acquiring process's pid: "
        f"{lock_path.read_text()!r} != {winners[0]['pid']}"
    )


def test_stale_takeover_race_never_double_acquires(tmp_path) -> None:
    """RED (pre-fix): reclaiming a crashed writer's lock gives it to ONE process.

    This is the production shape of the defect, with nothing simulated: the
    lock file names a pid that really died, so every contender is entitled
    to take it over. The pre-fix code reached its verdict ("holder is dead")
    and then unlinked the file as two separate steps, so a contender that
    created the lock in between had its brand-new claim deleted, and two
    processes ended up believing they owned the account.

    The fix makes the verdict and the takeover a single atomic step, so the
    second contender is refused no matter how the two interleave.
    """
    lock_path = tmp_path / "dhan.writer.lock"
    contenders = 12
    _dead_writer_lock(lock_path)  # crashed writer: the pid is reaped

    procs = _start_contenders(
        lock_path, contenders, hold_s=2.0, retries=8, start_delay=1.5
    )
    outcomes = _collect(procs)
    errors = [o for o in outcomes if o["outcome"] == "error"]
    assert not errors, f"unexpected contender errors: {errors!r}"

    acquired = [o for o in outcomes if o["outcome"] == "acquired"]
    refused = [o for o in outcomes if o["outcome"] == "refused"]
    assert len(acquired) == 1, (
        f"TOCTOU: stale lock handed to {len(acquired)} of {contenders} racing "
        f"processes {[o['pid'] for o in acquired]}; exactly one may acquire. "
        f"refused={[o['pid'] for o in refused]}"
    )
    # Every loser really was a loser, never a second winner.
    assert not ({o["pid"] for o in acquired} & {o["pid"] for o in refused})


# --------------------------------------------------------------------------
# Non-racing behaviours that must not regress
# --------------------------------------------------------------------------


def test_live_holder_still_refused(tmp_path) -> None:
    """A real lock held by a live process is still fail-closed.

    The holder is a genuine ``SingleWriterLock`` in another interpreter, so
    this is the real "two live boots, one account" guard, not a fixture:
    two holders are started, exactly one wins, and this process must be
    refused while that winner is still holding.
    """
    lock_path = tmp_path / "dhan.writer.lock"
    # The winner holds the lock for a long time and does not release, so the
    # file inspected below is unambiguously the winner's own claim.
    procs = _start_contenders(
        lock_path, 2, hold_s=4.0, retries=1, release_when_done=False
    )
    try:
        # Wait until a winner is in place, then give the loser time to be
        # refused, so the race is settled before this process contends.
        deadline = time.time() + 30
        while time.time() < deadline and not lock_path.exists():
            time.sleep(0.01)
        assert lock_path.exists(), "no contender ever took the lock"
        time.sleep(0.3)

        with pytest.raises(WriterLockHeldError, match="live writer"):
            SingleWriterLock(lock_path).acquire()
        # The holder's claim is untouched and still names a live pid.
        holder = int(lock_path.read_text().strip())
        assert holder > 0
        assert _pid_exists(holder)
    finally:
        for proc in procs:
            proc.kill()
            proc.communicate(timeout=30)


def test_live_pid_in_unlocked_file_still_refused(tmp_path) -> None:
    """A lock file naming a live pid but holding no lease is still refused.

    The flock is the primary claim; the recorded pid remains a fail-closed
    fallback for a lock written by a build that never flocked. Refusing
    here is also what stops a crashed writer whose pid was recycled from
    being mistaken for reclaimable.
    """
    lock_path = tmp_path / "dhan.writer.lock"
    holder_pid = os.getpid()
    _live_writer_lock(lock_path, holder_pid)

    lock = SingleWriterLock(lock_path)
    with pytest.raises(WriterLockHeldError, match="live writer"):
        lock.acquire()
    # The holder's file is untouched.
    assert lock_path.read_text().strip() == str(holder_pid)


def test_stale_lock_is_reclaimable(tmp_path) -> None:
    """A genuinely dead holder is reclaimed (auto-clear)."""
    lock_path = tmp_path / "dhan.writer.lock"
    dead = _dead_writer_lock(lock_path)  # reaped: os.kill(dead, 0) fails

    lock = SingleWriterLock(lock_path)
    lock.acquire()
    try:
        assert lock_path.read_text().strip() == str(os.getpid())
    finally:
        lock.release()


def test_lock_file_removed_on_release(tmp_path) -> None:
    """release() removes the lock file (existing behaviour)."""
    lock_path = tmp_path / "dhan.writer.lock"
    lock = SingleWriterLock(lock_path)
    lock.acquire()
    assert lock_path.exists()
    lock.release()
    assert not lock_path.exists()
    # release() is idempotent.
    lock.release()


def test_first_lock_leaks_after_second_close(monkeypatch, tmp_path) -> None:
    """RED: each RuntimeContext owns its writer_lock. Closing the
    second context does NOT release the first's lock (no shared
    global). The pre-fix code's _ACTIVE_WRITER_LOCK clobber meant
    closing the second context would release the first's lock by
    accident. After the fix, the first context must release
    itself.

    The two boots use *different* broker ids (DHAN vs UPSTOX) so
    their lockfile paths are distinct — this isolates the
    per-context ownership contract from the process-wide "only one
    live writer per account" rule. The test still uses a single
    mocked broker, since the lock contract is what we're testing.
    """
    locks: list[SingleWriterLock] = []

    class _CountingLock:
        def __init__(self, path):
            self._lock = SingleWriterLock(path)
            self.released = False
            locks.append(self)

        def acquire(self):
            return self._lock.acquire()

        def release(self):
            self.released = True
            return self._lock.release()

    monkeypatch.setattr(
        "tradex_runtime.writer_lock.SingleWriterLock", _CountingLock,
    )

    def _boot_with(broker_id: BrokerId) -> RuntimeContext:
        broker = MagicMock()
        broker.capabilities = MagicMock()
        broker.capabilities.max_stream_instruments = 1000
        broker.capabilities.depth_levels = 0
        backend = MagicMock()
        backend.subscribe_orders = MagicMock()
        broker.stream_backend.return_value = backend
        broker.master_loader = None
        broker.connect = MagicMock()
        broker.close = MagicMock()
        broker.disconnect = MagicMock()
        broker.get_orderbook = MagicMock(return_value=[])
        broker.get_positions = MagicMock(return_value=[])
        monkeypatch.setattr(
            "tradex_runtime.live.build_broker_from_env",
            lambda _pid, **_kw: broker,
        )
        cfg = AppConfig(mode="live", broker_id=broker_id, live_enabled=True,
                        persistence=PersistenceConfig(path=f"{tmp_path}/test-orders-{broker_id.value}.db"))
        return startup_mod.boot_context(cfg)

    rc1 = _boot_with(BrokerId.DHAN)
    rc2 = _boot_with(BrokerId.UPSTOX)

    # Both locks are acquired.
    assert len(locks) == 2
    assert all(not l.released for l in locks)

    # Closing the second context releases only the second's lock.
    rc2.close()
    assert not locks[0].released, (
        "M7: closing the second RuntimeContext must NOT release the "
        "first's writer_lock (each context owns its own)"
    )
    assert locks[1].released, (
        "M7: closing the second RuntimeContext must release its own lock"
    )

    # Closing the first context releases the first's lock.
    rc1.close()
    assert locks[0].released, (
        "M7: closing the first RuntimeContext must release its own lock"
    )


def test_runtime_context_carries_its_own_writer_lock(monkeypatch, tmp_path) -> None:
    """M7: each RuntimeContext holds a local writer_lock reference, not the
    module global. After the second boot, the first RuntimeContext's
    ``self.writer_lock`` still points to the first lock (not the second).
    """
    locks: list[SingleWriterLock] = []

    class _CountingLock:
        def __init__(self, path):
            self._lock = SingleWriterLock(path)
            self.released = False
            locks.append(self)

        def acquire(self):
            return self._lock.acquire()

        def release(self):
            self.released = True
            return self._lock.release()

    monkeypatch.setattr(
        "tradex_runtime.writer_lock.SingleWriterLock", _CountingLock,
    )

    def _boot_with(broker_id: BrokerId):
        broker = MagicMock()
        broker.capabilities = MagicMock()
        broker.capabilities.max_stream_instruments = 1000
        broker.capabilities.depth_levels = 0
        backend = MagicMock()
        backend.subscribe_orders = MagicMock()
        broker.stream_backend.return_value = backend
        broker.master_loader = None
        broker.connect = MagicMock()
        broker.close = MagicMock()
        broker.disconnect = MagicMock()
        broker.get_orderbook = MagicMock(return_value=[])
        broker.get_positions = MagicMock(return_value=[])
        monkeypatch.setattr(
            "tradex_runtime.live.build_broker_from_env",
            lambda _pid, **_kw: broker,
        )
        cfg = AppConfig(mode="live", broker_id=broker_id, live_enabled=True,
                        persistence=PersistenceConfig(path=f"{tmp_path}/test-orders-{broker_id.value}.db"))
        return startup_mod.boot_context(cfg)

    rc1 = _boot_with(BrokerId.DHAN)
    rc2 = _boot_with(BrokerId.UPSTOX)

    # M7: each context holds its own writer_lock (not the global).
    assert rc1.writer_lock is locks[0]
    assert rc2.writer_lock is locks[1]
    # And they are different objects — no shared state.
    assert rc1.writer_lock is not rc2.writer_lock

    rc1.close()
    rc2.close()
