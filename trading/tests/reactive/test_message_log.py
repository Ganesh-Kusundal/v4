"""Tests for FileMessageLog."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

import pytest

from tradex_trading.reactive.message_log import FileMessageLog


@pytest.fixture()
def log_path(tmp_path):
    return tmp_path / "events.jsonl"


# ------------------------------------------------------------------ #
# 1. append + replay
# ------------------------------------------------------------------ #

def test_append_and_replay(log_path):
    log = FileMessageLog(log_path)
    msgs = [
        {"event": "A", "timestamp": datetime.now(UTC).isoformat()},
        {"event": "B", "timestamp": datetime.now(UTC).isoformat()},
        {"event": "C", "timestamp": datetime.now(UTC).isoformat()},
    ]
    for m in msgs:
        log.append(m)

    results = list(log.replay())
    assert len(results) == 3
    assert results[0]["event"] == "A"
    assert results[2]["event"] == "C"


# ------------------------------------------------------------------ #
# 2. replay_range
# ------------------------------------------------------------------ #

def test_replay_range(log_path):
    log = FileMessageLog(log_path)
    base = datetime(2026, 1, 1, tzinfo=UTC)
    for i in range(5):
        log.append({"ts": i, "timestamp": (base + timedelta(hours=i)).isoformat()})

    start = base + timedelta(hours=1)
    end = base + timedelta(hours=3)
    filtered = list(log.replay_range(start=start, end=end))
    assert len(filtered) == 3
    assert filtered[0]["ts"] == 1
    assert filtered[-1]["ts"] == 3


# ------------------------------------------------------------------ #
# 3. count property
# ------------------------------------------------------------------ #

def test_count_property(log_path):
    log = FileMessageLog(log_path)
    assert log.count == 0
    for i in range(4):
        log.append({"i": i})
    assert log.count == 4


# ------------------------------------------------------------------ #
# 4. empty file replay
# ------------------------------------------------------------------ #

def test_empty_file_replay(log_path):
    log = FileMessageLog(log_path)
    assert list(log.replay()) == []


# ------------------------------------------------------------------ #
# 5. dict messages
# ------------------------------------------------------------------ #

def test_dict_messages(log_path):
    log = FileMessageLog(log_path)
    log.append({"key": "value", "n": 42})
    results = list(log.replay())
    assert len(results) == 1
    assert results[0]["key"] == "value"
    assert results[0]["n"] == 42


# ------------------------------------------------------------------ #
# 6. file created if not exists
# ------------------------------------------------------------------ #

def test_file_created_if_not_exists(tmp_path):
    nested = tmp_path / "sub" / "dir" / "events.jsonl"
    assert not nested.exists()
    log = FileMessageLog(nested)
    assert nested.exists()
    assert log.count == 0
