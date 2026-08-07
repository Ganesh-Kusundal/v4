"""Ported from v3 ``test_totp_cooldown.py`` — TotpCooldownGuard.

v4 ``TotpCooldownGuard`` supports two APIs:

* **v4 in-process API** — ``acquire()``, ``record_attempt()``,
  ``wait_and_acquire()``, ``remaining_cooldown`` (monotonic clock, no
  persistence).
* **v3 durable API** — ``for_broker()``, ``check_allowed()``,
  ``acquire_attempt()``, ``record_success()``, ``record_rate_limited()``,
  ``remaining_cooldown_seconds()`` (wall-clock, cross-process JSON state).
"""

from __future__ import annotations

import json
import time
from pathlib import Path

import pytest
from tradex_domain import RateLimitError

from tradex_brokers.common.totp_cooldown import (
    BROKER_COOLDOWN_SECONDS,
    DHAN_COOLDOWN_SECONDS,
    UPSTOX_COOLDOWN_SECONDS,
    TotpCooldownGuard,
    TotpRateLimitError,
)

# ---------------------------------------------------------------------------
# TotpRateLimitError
# ---------------------------------------------------------------------------


def test_totp_rate_limit_error_carries_remaining_seconds_and_subclasses_rate_limit() -> None:
    exc = TotpRateLimitError("blocked", remaining_seconds=42.0)
    assert isinstance(exc, RateLimitError)
    assert exc.remaining_seconds == 42.0


# ---------------------------------------------------------------------------
# Broker cooldown constants
# ---------------------------------------------------------------------------


def test_broker_cooldown_constants() -> None:
    assert DHAN_COOLDOWN_SECONDS == 120.0
    assert UPSTOX_COOLDOWN_SECONDS == 600.0
    assert BROKER_COOLDOWN_SECONDS["dhan"] == DHAN_COOLDOWN_SECONDS
    assert BROKER_COOLDOWN_SECONDS["upstox"] == UPSTOX_COOLDOWN_SECONDS


# ---------------------------------------------------------------------------
# Basic acquire / record_attempt (v4 in-process API)
# ---------------------------------------------------------------------------


def test_acquire_allowed_initially() -> None:
    guard = TotpCooldownGuard(cooldown_seconds=60.0)
    assert guard.acquire() is True


def test_acquire_blocked_immediately_after_attempt() -> None:
    guard = TotpCooldownGuard(cooldown_seconds=60.0)
    guard.record_attempt()
    assert guard.acquire() is False


def test_acquire_allowed_after_cooldown_elapses() -> None:
    guard = TotpCooldownGuard(cooldown_seconds=0.05)
    guard.record_attempt()
    assert guard.acquire() is False
    time.sleep(0.1)
    assert guard.acquire() is True


def test_remaining_cooldown_decreases_over_time() -> None:
    guard = TotpCooldownGuard(cooldown_seconds=60.0)
    assert guard.remaining_cooldown == 0.0
    guard.record_attempt()
    remaining = guard.remaining_cooldown
    assert remaining > 0.0
    assert remaining <= 60.0


def test_remaining_cooldown_zero_when_no_attempts() -> None:
    guard = TotpCooldownGuard(cooldown_seconds=60.0)
    assert guard.remaining_cooldown == 0.0


def test_zero_cooldown_always_allows() -> None:
    guard = TotpCooldownGuard(cooldown_seconds=0.0)
    guard.record_attempt()
    assert guard.acquire() is True
    assert guard.remaining_cooldown == 0.0


# ---------------------------------------------------------------------------
# wait_and_acquire
# ---------------------------------------------------------------------------


def test_wait_and_acquire_returns_true_when_ready() -> None:
    guard = TotpCooldownGuard(cooldown_seconds=0.05)
    guard.record_attempt()
    assert guard.wait_and_acquire(timeout=0.5) is True


def test_wait_and_acquire_returns_false_on_timeout() -> None:
    guard = TotpCooldownGuard(cooldown_seconds=60.0)
    guard.record_attempt()
    assert guard.wait_and_acquire(timeout=0.01) is False


# ---------------------------------------------------------------------------
# Invalid construction
# ---------------------------------------------------------------------------


def test_negative_cooldown_rejected() -> None:
    with pytest.raises(ValueError, match="non-negative"):
        TotpCooldownGuard(cooldown_seconds=-1.0)


# ---------------------------------------------------------------------------
# Durable API — acquire_attempt / check_allowed / remaining_cooldown_seconds
# ---------------------------------------------------------------------------


def test_acquire_attempt_blocks_second_attempt_with_remaining_seconds(
    tmp_path: Path,
) -> None:
    guard = TotpCooldownGuard(broker="dhan", state_path=tmp_path / "cd.json")
    reserved_at = guard.acquire_attempt()
    assert reserved_at > 0

    with pytest.raises(TotpRateLimitError) as excinfo:
        guard.acquire_attempt()
    assert 0 < excinfo.value.remaining_seconds <= DHAN_COOLDOWN_SECONDS
    assert "cooldown" in str(excinfo.value).lower()


def test_acquire_attempt_allowed_after_window_elapses(tmp_path: Path) -> None:
    guard = TotpCooldownGuard(
        broker="test-broker", cooldown_seconds=0.05, state_path=tmp_path / "cd.json"
    )
    guard.acquire_attempt()
    with pytest.raises(TotpRateLimitError):
        guard.acquire_attempt()
    time.sleep(0.1)
    guard.acquire_attempt()  # window elapsed -> allowed again


def test_release_attempt_clears_reservation(tmp_path: Path) -> None:
    guard = TotpCooldownGuard(broker="dhan", state_path=tmp_path / "cd.json")
    reserved_at = guard.acquire_attempt()
    guard.release_attempt(reserved_at)
    assert guard.remaining_cooldown_seconds() == 0.0
    guard.acquire_attempt()  # a failed attempt (network error) must not count


def test_record_rate_limited_blocks_following_attempts(tmp_path: Path) -> None:
    guard = TotpCooldownGuard(broker="dhan", state_path=tmp_path / "cd.json")
    guard.record_rate_limited()
    with pytest.raises(TotpRateLimitError):
        guard.acquire_attempt()


def test_record_success_starts_window_then_expires(tmp_path: Path) -> None:
    guard = TotpCooldownGuard(
        broker="test-broker", cooldown_seconds=0.05, state_path=tmp_path / "cd.json"
    )
    guard.record_success()
    assert guard.remaining_cooldown_seconds() > 0.0
    with pytest.raises(TotpRateLimitError):
        guard.acquire_attempt()
    time.sleep(0.1)
    guard.acquire_attempt()  # window expires -> allowed


def test_remaining_is_zero_and_check_allowed_without_attempts(tmp_path: Path) -> None:
    guard = TotpCooldownGuard(broker="dhan", state_path=tmp_path / "cd.json")
    assert guard.remaining_cooldown_seconds() == 0.0
    guard.check_allowed()  # no raise


# ---------------------------------------------------------------------------
# for_broker singleton
# ---------------------------------------------------------------------------


def test_for_broker_returns_shared_instance() -> None:
    # Clear singleton cache to avoid cross-test pollution
    TotpCooldownGuard._instances.clear()
    first = TotpCooldownGuard.for_broker("dhan")
    second = TotpCooldownGuard.for_broker("dhan")
    assert first is second
    assert first._cooldown_seconds == DHAN_COOLDOWN_SECONDS
    # Cleanup
    TotpCooldownGuard._instances.clear()


# ---------------------------------------------------------------------------
# Durability: state persists across instances
# ---------------------------------------------------------------------------


def test_guard_state_persists_across_instances(tmp_path: Path) -> None:
    state = tmp_path / "cd.json"
    first = TotpCooldownGuard(broker="dhan", state_path=state)
    first.record_rate_limited()

    # A fresh instance (a different manager/process) reads the same state file.
    second = TotpCooldownGuard(broker="dhan", state_path=state)
    with pytest.raises(TotpRateLimitError) as excinfo:
        second.acquire_attempt()
    assert excinfo.value.remaining_seconds > 0


def test_legacy_rate_limited_at_field_is_migrated(tmp_path: Path) -> None:
    state = tmp_path / "cd.json"
    state.write_text(json.dumps({"rate_limited_at": time.time()}))
    guard = TotpCooldownGuard(broker="dhan", state_path=state)
    with pytest.raises(TotpRateLimitError):
        guard.acquire_attempt()


def test_persist_state_writes_json(tmp_path: Path) -> None:
    state = tmp_path / "cd.json"
    guard = TotpCooldownGuard(broker="dhan", state_path=state)
    guard.record_success()
    assert state.exists()
    data = json.loads(state.read_text())
    assert data["broker"] == "dhan"
    assert data["last_success_at"] is not None
    assert data["last_attempt_at"] is not None
