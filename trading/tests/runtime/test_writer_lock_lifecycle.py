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
"""

from __future__ import annotations

from unittest.mock import MagicMock

from tradex_domain import BrokerId
from tradex_trading.config.schema import AppConfig
from tradex_trading.runtime import startup as startup_mod
from tradex_trading.runtime.startup import RuntimeContext
from tradex_trading.runtime.writer_lock import SingleWriterLock


def test_first_lock_leaks_after_second_close(monkeypatch) -> None:
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
        "tradex_trading.runtime.writer_lock.SingleWriterLock", _CountingLock,
    )

    def _boot_with(broker_id: BrokerId) -> RuntimeContext:
        broker = MagicMock()
        broker.capabilities = MagicMock()
        broker.capabilities.max_stream_instruments = 1000
        broker.capabilities.depth_levels = 0
        broker.stream_backend.return_value = None
        broker.master_loader = None
        broker.connect = MagicMock()
        broker.close = MagicMock()
        broker.disconnect = MagicMock()
        broker.get_orderbook = MagicMock(return_value=[])
        broker.get_positions = MagicMock(return_value=[])
        monkeypatch.setattr(
            "tradex_trading.runtime.live.build_broker_from_env",
            lambda _pid, **_kw: broker,
        )
        cfg = AppConfig(mode="live", broker_id=broker_id, live_enabled=True)
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


def test_runtime_context_carries_its_own_writer_lock(monkeypatch) -> None:
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
        "tradex_trading.runtime.writer_lock.SingleWriterLock", _CountingLock,
    )

    def _boot_with(broker_id: BrokerId):
        broker = MagicMock()
        broker.capabilities = MagicMock()
        broker.capabilities.max_stream_instruments = 1000
        broker.capabilities.depth_levels = 0
        broker.stream_backend.return_value = None
        broker.master_loader = None
        broker.connect = MagicMock()
        broker.close = MagicMock()
        broker.disconnect = MagicMock()
        broker.get_orderbook = MagicMock(return_value=[])
        broker.get_positions = MagicMock(return_value=[])
        monkeypatch.setattr(
            "tradex_trading.runtime.live.build_broker_from_env",
            lambda _pid, **_kw: broker,
        )
        cfg = AppConfig(mode="live", broker_id=broker_id, live_enabled=True)
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

