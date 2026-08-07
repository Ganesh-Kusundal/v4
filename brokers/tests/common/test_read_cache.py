"""Tests for the ReadCache TTL expiry, eviction, and key-specific invalidation."""

from __future__ import annotations

import time

import pytest

from tradex_brokers.common.cache import ReadCache

# ---------------------------------------------------------------------------
# Tests
# ---------------------------------------------------------------------------


class TestReadCache:
    """ReadCache TTL, eviction, and invalidation behaviour."""

    def test_cache_ttl_expiry_returns_miss(self) -> None:
        """After TTL elapses, get() returns None (cache miss)."""
        cache = ReadCache(max_entries=10)
        cache.set("key1", {"data": "value"}, ttl_seconds=0.05)

        # Immediate hit
        assert cache.get("key1") == {"data": "value"}

        # Wait for TTL to expire
        time.sleep(0.1)
        assert cache.get("key1") is None
        assert cache.misses >= 1

    def test_cache_evicts_oldest_when_full(self) -> None:
        """When the cache is full, the oldest entry (earliest expires_at) is evicted."""
        cache = ReadCache(max_entries=2)
        # Insert two entries; first has shorter TTL (expires first)
        cache.set("oldest", "first-value", ttl_seconds=1.0)
        cache.set("newer", "second-value", ttl_seconds=10.0)
        assert cache.size == 2

        # Insert a third — should evict "oldest" (earliest expires_at)
        cache.set("newest", "third-value", ttl_seconds=10.0)
        assert cache.size == 2
        assert cache.get("oldest") is None
        assert cache.get("newer") == "second-value"
        assert cache.get("newest") == "third-value"

    def test_cache_key_specific_invalidation(self) -> None:
        """invalidate(key) removes only the specified key, leaving others intact."""
        cache = ReadCache(max_entries=10)
        cache.set("a", 1, ttl_seconds=60.0)
        cache.set("b", 2, ttl_seconds=60.0)
        cache.set("c", 3, ttl_seconds=60.0)

        cache.invalidate(key="b")
        assert cache.get("a") == 1
        assert cache.get("b") is None
        assert cache.get("c") == 3
        assert cache.size == 2


class TestReadCacheInvalidation:
    """ReadCache regex / full invalidation and counters."""

    def test_invalidate_none_clears_all(self) -> None:
        cache = ReadCache(max_entries=10)
        for i in range(5):
            cache.set(f"k{i}", i, ttl_seconds=60.0)
        removed = cache.invalidate()  # key=None -> drop every entry
        assert removed == 5
        assert cache.size == 0

    def test_invalidate_regex_pattern(self) -> None:
        cache = ReadCache(max_entries=10)
        cache.set("foo:1", 1, ttl_seconds=60.0)
        cache.set("foo:2", 2, ttl_seconds=60.0)
        cache.set("bar:1", 3, ttl_seconds=60.0)
        removed = cache.invalidate(key=r"foo:\d+")
        assert removed == 2
        assert cache.get("foo:1") is None
        assert cache.get("foo:2") is None
        assert cache.get("bar:1") == 3

    def test_invalidate_missing_literal_returns_zero(self) -> None:
        cache = ReadCache(max_entries=10)
        assert cache.invalidate(key="missing-key") == 0
        assert cache.size == 0

    def test_invalidate_missing_regex_returns_zero(self) -> None:
        cache = ReadCache(max_entries=10)
        cache.set("foo", 1, ttl_seconds=60.0)
        # A regex that matches nothing.
        assert cache.invalidate(key=r"nonexistent:\d+") == 0
        assert cache.size == 1


class TestReadCacheMetrics:
    """ReadCache hit/miss/expired counters and size."""

    def test_hits_misses_and_expired_counted(self) -> None:
        cache = ReadCache(max_entries=10, clock=lambda: 0.0)
        cache.set("k", "v", ttl_seconds=60.0)
        assert cache.get("k") == "v"  # hit
        assert cache.hits == 1
        assert cache.misses == 0

        assert cache.get("missing") is None  # miss
        assert cache.misses == 1

    def test_expired_entry_counts_expired_and_miss(self) -> None:
        clock = {"t": 0.0}

        def now() -> float:
            return clock["t"]

        cache = ReadCache(max_entries=10, clock=now)
        cache.set("k", "v", ttl_seconds=10.0)
        clock["t"] = 20.0  # TTL elapsed
        assert cache.get("k") is None
        assert cache.expired_count == 1
        assert cache.misses == 1

    def test_size_reflects_entries(self) -> None:
        cache = ReadCache(max_entries=10)
        assert cache.size == 0
        cache.set("a", 1, ttl_seconds=60.0)
        cache.set("b", 2, ttl_seconds=60.0)
        assert cache.size == 2


class TestReadCacheGuards:
    """ReadCache construction and set guards."""

    def test_nonpositive_max_entries_rejected(self) -> None:
        with pytest.raises(ValueError):
            ReadCache(max_entries=0)
        with pytest.raises(ValueError):
            ReadCache(max_entries=-1)

    def test_nonpositive_ttl_does_not_set(self) -> None:
        cache = ReadCache(max_entries=10)
        cache.set("k", "v", ttl_seconds=0)
        assert cache.size == 0
        assert cache.get("k") is None

    def test_eviction_only_when_key_new(self) -> None:
        """Updating an existing key never evicts a different entry."""
        cache = ReadCache(max_entries=1)
        cache.set("a", 1, ttl_seconds=60.0)
        cache.set("a", 2, ttl_seconds=60.0)  # update, not eviction
        assert cache.size == 1
        assert cache.get("a") == 2
