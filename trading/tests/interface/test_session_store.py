"""Unit tests for InMemorySessionStore — B8 session auth."""
from __future__ import annotations

from datetime import UTC, datetime, timedelta

import pytest

from tradex_trading.interface.auth.session_store import (
    SESSION_ABSOLUTE_TTL,
    SESSION_IDLE_TTL,
    InMemorySessionStore,
)


# ---------------------------------------------------------------------------
# create / lookup basics
# ---------------------------------------------------------------------------


def test_create_returns_64_char_hex_id_and_record() -> None:
    store = InMemorySessionStore()
    raw_id, record = store.create("operator")
    assert isinstance(raw_id, str)
    assert len(raw_id) == 64  # 32 bytes → 64 hex chars
    assert record.subject == "operator"
    assert not record.revoked
    assert record.auth_version == 0


def test_raw_id_is_not_stored_in_record() -> None:
    """Store must hold only the hash, never the bearer value."""
    store = InMemorySessionStore()
    raw_id, record = store.create("operator")
    assert record.session_id_hash != raw_id
    # Confirm the raw value is absent from every stored record's hash field
    with store._lock:
        for r in store._records.values():
            assert raw_id not in r.session_id_hash


def test_lookup_valid_session_returns_record() -> None:
    store = InMemorySessionStore()
    raw_id, _ = store.create("operator")
    record = store.lookup(raw_id)
    assert record is not None
    assert record.subject == "operator"


def test_lookup_unknown_id_returns_none() -> None:
    store = InMemorySessionStore()
    assert store.lookup("not-a-real-session-id") is None


def test_lookup_empty_string_returns_none() -> None:
    store = InMemorySessionStore()
    assert store.lookup("") is None


# ---------------------------------------------------------------------------
# Revocation
# ---------------------------------------------------------------------------


def test_revoke_valid_returns_true() -> None:
    store = InMemorySessionStore()
    raw_id, _ = store.create("operator")
    assert store.revoke(raw_id) is True


def test_revoke_unknown_returns_false() -> None:
    store = InMemorySessionStore()
    assert store.revoke("nonexistent") is False


def test_lookup_after_revoke_returns_none() -> None:
    store = InMemorySessionStore()
    raw_id, _ = store.create("operator")
    store.revoke(raw_id)
    assert store.lookup(raw_id) is None


# ---------------------------------------------------------------------------
# Expiry
# ---------------------------------------------------------------------------


def test_lookup_after_absolute_expiry_returns_none() -> None:
    store = InMemorySessionStore()
    raw_id, record = store.create("operator")
    # Fast-forward: expire the record
    record.expires_at = datetime.now(UTC) - timedelta(seconds=1)
    assert store.lookup(raw_id) is None


def test_lookup_after_idle_expiry_returns_none() -> None:
    store = InMemorySessionStore()
    raw_id, record = store.create("operator")
    record.idle_expires_at = datetime.now(UTC) - timedelta(seconds=1)
    assert store.lookup(raw_id) is None


def test_lookup_refreshes_idle_expiry() -> None:
    store = InMemorySessionStore()
    raw_id, record = store.create("operator")
    # Advance idle expiry close to expiration
    record.idle_expires_at = datetime.now(UTC) + timedelta(seconds=5)
    before = record.idle_expires_at
    store.lookup(raw_id)  # should refresh
    assert record.idle_expires_at > before


# ---------------------------------------------------------------------------
# active_count
# ---------------------------------------------------------------------------


def test_active_count_reflects_live_sessions() -> None:
    store = InMemorySessionStore()
    assert store.active_count() == 0
    raw_id, _ = store.create("operator")
    assert store.active_count() == 1
    store.revoke(raw_id)
    assert store.active_count() == 0


def test_active_count_does_not_count_multiple_revocations() -> None:
    store = InMemorySessionStore()
    raw_id, _ = store.create("operator")
    store.revoke(raw_id)
    store.revoke(raw_id)  # idempotent
    assert store.active_count() == 0


# ---------------------------------------------------------------------------
# CSRF secret isolation
# ---------------------------------------------------------------------------


def test_each_session_gets_distinct_csrf_secret() -> None:
    store = InMemorySessionStore()
    _, r1 = store.create("operator")
    _, r2 = store.create("operator")
    assert r1.csrf_secret != r2.csrf_secret
