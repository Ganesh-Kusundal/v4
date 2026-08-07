"""Ported from v3 ``test_infra_log_bus.py`` — message log + deserialization.

v4 ``InMemoryMessageLog``, ``SQLiteMessageLog``, ``MessageEnvelope``, and
``_deserialize`` are API-compatible with v3.  EventBus tests are omitted
because v4 uses ``ReactiveBus`` (covered in the trading-layer tests).
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

from tradex_brokers.common.message_log import (
    InMemoryMessageLog,
    MessageEnvelope,
    SQLiteMessageLog,
    _deserialize,
)


def _now() -> datetime:
    return datetime.now(UTC)


# ---------------------------------------------------------------------------
# _deserialize helper
# ---------------------------------------------------------------------------


def test_deserialize_invalid_json_falls_back_to_raw() -> None:
    assert _deserialize("not-json{{") == "not-json{{"
    assert _deserialize('{"ok": true}') == {"ok": True}


# ---------------------------------------------------------------------------
# InMemoryMessageLog — filters + session
# ---------------------------------------------------------------------------


def test_in_memory_read_with_start_end_filters() -> None:
    log = InMemoryMessageLog()
    log.append({"m": 1})
    log.append({"m": 2})
    entries = list(log.read(start=_now() - timedelta(minutes=1), end=_now()))
    assert len(entries) == 2
    assert all(isinstance(e, MessageEnvelope) for e in entries)


def test_in_memory_read_session_filters_by_id() -> None:
    log = InMemoryMessageLog()
    log.append({"session_id": "s1", "m": 1})
    log.append({"session_id": "s2", "m": 2})
    log.append({"m": 3})
    assert [e["m"] for e in log.read_session("s1")] == [1]
    assert [e["m"] for e in log.read_session("s2")] == [2]
    assert list(log.read_session("missing")) == []


def test_in_memory_log_round_trip() -> None:
    log = InMemoryMessageLog()
    log.append({"ts": "2026-07-01T10:00:00Z", "msg": 1})
    log.append({"ts": "2026-07-01T10:00:01Z", "msg": 2})
    entries = list(log.read())
    assert all(isinstance(e, MessageEnvelope) for e in entries)
    assert [e.message for e in entries] == [
        {"ts": "2026-07-01T10:00:00Z", "msg": 1},
        {"ts": "2026-07-01T10:00:01Z", "msg": 2},
    ]
    log.clear()
    assert list(log.read()) == []
    log.close()


# ---------------------------------------------------------------------------
# SQLiteMessageLog — append / read / session / clear / close
# ---------------------------------------------------------------------------


def test_sqlite_serialization_variants(tmp_path) -> None:
    log = SQLiteMessageLog(tmp_path / "variants.db")
    log.append({"session_id": "s1", "payload": 10})
    log.append({"no_session": True})
    log.append("plain-string")
    entries = list(log.read())
    assert len(entries) == 3
    assert entries[2].message == {"__repr__": "'plain-string'"}
    assert list(log.read_session("s1"))[0] == {"session_id": "s1", "payload": 10}
    log.close()


def test_sqlite_read_with_range_and_clear(tmp_path) -> None:
    log = SQLiteMessageLog(tmp_path / "range.db")
    log.append({"m": 1})
    log.append({"m": 2})
    start = _now() - timedelta(minutes=1)
    end = _now() + timedelta(minutes=1)
    assert len(list(log.read(start=start, end=end))) == 2
    log.clear()
    assert list(log.read()) == []
    log.close()


def test_sqlite_log_session_and_close(tmp_path) -> None:
    log = SQLiteMessageLog(tmp_path / "messages.db")
    log.append({"session_id": "s1", "payload": 10})
    log.append({"session_id": "s1", "payload": 20})
    log.append({"session_id": "s2", "payload": 30})
    session = list(log.read_session("s1"))
    assert len(session) == 2
    log.close()
    log.close()  # idempotent
