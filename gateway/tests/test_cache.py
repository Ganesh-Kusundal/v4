from __future__ import annotations

import threading
import time

from tradex_gateway.cache import SingleFlight, TTLCache


def test_ttl_cache_serves_values_until_expiry():
    cache = TTLCache()
    cache.put(("k",), {"value": 1}, ttl=5.0, now=100.0)
    assert cache.get(("k",), 104.0) == {"value": 1}
    assert cache.get(("k",), 105.0) is None
    assert cache.get(("k",), 106.0) is None


def test_ttl_cache_evicts_the_soonest_expiry_when_full():
    cache = TTLCache(maxsize=2)
    cache.put("a", 1, ttl=10.0, now=100.0)
    cache.put("b", 2, ttl=50.0, now=100.0)
    cache.put("c", 3, ttl=20.0, now=100.0)
    assert cache.get("a", 101.0) is None
    assert cache.get("b", 101.0) == 2
    assert cache.get("c", 101.0) == 3


def test_single_flight_removes_idle_entries():
    flight = SingleFlight()
    with flight.lock("k"):
        assert set(flight.entries) == {"k"}
    assert flight.entries == {}


def test_single_flight_serializes_waiters_and_clears_entries():
    flight = SingleFlight()
    first_inside = threading.Event()
    release_first = threading.Event()
    second_inside = threading.Event()
    entries_seen_by_second: list[int] = []
    active = 0
    peak = 0

    def worker(first: bool) -> None:
        nonlocal active, peak
        with flight.lock("k"):
            active += 1
            peak = max(peak, active)
            if first:
                first_inside.set()
                release_first.wait(5.0)
            else:
                entries_seen_by_second.append(len(flight.entries))
                second_inside.set()
            active -= 1

    first = threading.Thread(target=worker, args=(True,))
    first.start()
    assert first_inside.wait(5.0)
    second = threading.Thread(target=worker, args=(False,))
    second.start()
    # Wait until the second worker has joined the flight entry as a waiter.
    queued = False
    deadline = time.monotonic() + 5.0
    while time.monotonic() < deadline:
        entry = flight.entries.get("k")
        if entry is not None and entry.users == 2:
            queued = True
            break
        time.sleep(0.001)
    assert queued
    # The queued waiter cannot enter while the first holder is active.
    assert not second_inside.is_set()
    release_first.set()
    first.join(5.0)
    second.join(5.0)
    assert not first.is_alive()
    assert not second.is_alive()
    assert peak == 1
    # The entry stays alive while the second holder is inside it.
    assert entries_seen_by_second == [1]
    assert flight.entries == {}
