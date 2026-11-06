"""MP-5: MemoryIdempotencyGuard._completed is bounded."""

from __future__ import annotations

from collections import OrderedDict

from tradex_domain.value_objects import CorrelationId
from tradex_execution.idempotency import MemoryIdempotencyGuard


def test_completed_map_is_ordered_dict() -> None:
    guard = MemoryIdempotencyGuard(max_completed=4)
    assert isinstance(guard._completed, OrderedDict)


def test_oldest_evicted_beyond_cap() -> None:
    cap = 4
    guard = MemoryIdempotencyGuard(max_completed=cap)
    keys = [f"k{i}" for i in range(cap + 2)]
    for k in keys:
        cid = CorrelationId(value=k)
        assert guard.check_and_reserve(cid) is None
        guard.record_result(cid, f"res-{k}")

    # Oldest two evicted.
    assert len(guard._completed) == cap
    assert "k0" not in guard._completed
    assert "k1" not in guard._completed
    # Newest retained.
    assert "k5" in guard._completed

    # Evicted key answers as fresh.
    cid0 = CorrelationId(value="k0")
    assert guard.check_and_reserve(cid0) is None


def test_default_cap_10000_retains_last_10000() -> None:
    guard = MemoryIdempotencyGuard()
    n = 10_001
    for i in range(n):
        cid = CorrelationId(value=f"k{i}")
        assert guard.check_and_reserve(cid) is None
        guard.record_result(cid, i)
    assert len(guard._completed) == 10_000
    assert "k0" not in guard._completed
    assert f"k{n - 1}" in guard._completed


def test_release_and_record_result_contract_unchanged() -> None:
    guard = MemoryIdempotencyGuard(max_completed=10)
    cid = CorrelationId(value="abc")
    assert guard.check_and_reserve(cid) is None
    guard.record_result(cid, "order-123")
    dup = guard.check_and_reserve(cid)
    assert dup is not None
    assert dup.result == "order-123"

    cid2 = CorrelationId(value="def")
    guard.check_and_reserve(cid2)
    guard.release(cid2)
    assert guard.check_and_reserve(cid2) is None
