from __future__ import annotations

import pytest

from tradex_trading.interface.replay_guard import ReplayGuard


def test_replay_guard_exclusive_acquire() -> None:
    guard = ReplayGuard()

    guard.acquire("run-a")
    assert guard.active is True

    with pytest.raises(RuntimeError, match="another replay"):
        guard.acquire("run-b")

    assert guard.active is True
    guard.release("run-a")
    assert guard.active is False

    guard.acquire("run-b")
    assert guard.active is True
    guard.release("run-b")


def test_replay_guard_same_run_id_is_idempotent() -> None:
    guard = ReplayGuard()
    guard.acquire("run-a")
    guard.acquire("run-a")
    assert guard.active is True
    guard.release("run-a")
    assert guard.active is False


def test_replay_guard_release_is_idempotent() -> None:
    guard = ReplayGuard()

    guard.acquire("run-a")
    guard.release("run-a")
    guard.release("run-a")

    assert guard.active is False


def test_replay_guard_rejects_empty_run_id() -> None:
    guard = ReplayGuard()

    with pytest.raises(ValueError):
        guard.acquire("   ")

    assert guard.active is False


def test_replay_guard_concurrent_acquire_release_is_thread_safe() -> None:
    """Concurrent exclusive acquire+release must never corrupt internal state."""
    import threading

    guard = ReplayGuard()
    errors: list[Exception] = []
    held = 0
    lock = threading.Lock()

    def _worker(run_id: str) -> None:
        nonlocal held
        try:
            for _ in range(200):
                try:
                    guard.acquire(run_id)
                except RuntimeError:
                    continue
                with lock:
                    held += 1
                    if held > 1:
                        raise AssertionError("two runs held at once")
                guard.release(run_id)
                with lock:
                    held -= 1
        except Exception as exc:  # noqa: BLE001
            errors.append(exc)

    threads = [threading.Thread(target=_worker, args=(f"run-{i}",)) for i in range(8)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()

    assert not errors, f"concurrent guard access raised: {errors}"
    assert not guard.active


def test_replay_guard_release_unknown_id_is_noop() -> None:
    """Releasing an id that was never acquired must not crash or flip active."""
    guard = ReplayGuard()
    guard.acquire("held")
    guard.release("never-acquired")  # must be a no-op
    assert guard.active is True
    guard.release("held")
    assert guard.active is False
