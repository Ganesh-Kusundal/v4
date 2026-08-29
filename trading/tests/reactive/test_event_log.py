"""Tests for the durable SQLite event log (R1).

Replaces the in-memory ``deque(maxlen=10_000)`` with a SQLite append-only
log that survives process restarts.
"""

from __future__ import annotations

import tempfile
from pathlib import Path

from tradex_trading.reactive.bus import ReactiveBus
from tradex_trading.reactive.event_log import SQLEventLog

# ---------------------------------------------------------------------------
# SQLEventLog: append + replay
# ---------------------------------------------------------------------------


class TestSQLEventLog:
    def test_sqlite_event_log_appends_and_replays(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "events.db"
            log = SQLEventLog(path)
            try:
                log.append("a")
                log.append("b")
                log.append("c")
                replayed = list(log.replay())
            finally:
                log.close()
            assert replayed == ["a", "b", "c"]

    def test_replay_after_id(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "events.db"
            log = SQLEventLog(path)
            try:
                for i in range(5):
                    log.append(f"e{i}")
                replayed = list(log.replay(after_id=2))
            finally:
                log.close()
            # ids 1..5, after_id=2 means return ids 3,4,5
            assert replayed == ["e2", "e3", "e4"]


# ---------------------------------------------------------------------------
# ReactiveBus + SQLEventLog integration
# ---------------------------------------------------------------------------


class TestReactiveBusWithSQLEventLog:
    def test_bus_uses_sqlite_log_when_provided(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "bus_events.db"
            bus = ReactiveBus(event_log=SQLEventLog(path))
            bus.publish("x")
            bus.publish("y")
            # Bus closes its log; reopen for replay verification
            bus._log.close()  # noqa: SLF001 — verify bus held the log

            reopened = SQLEventLog(path)
            try:
                replayed = list(reopened.replay())
            finally:
                reopened.close()
            assert replayed == ["x", "y"]

    def test_event_log_persists_across_instances(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "persist.db"
            log1 = SQLEventLog(path)
            try:
                log1.append("first")
                log1.append("second")
            finally:
                log1.close()

            log2 = SQLEventLog(path)
            try:
                replayed = list(log2.replay())
            finally:
                log2.close()
            assert replayed == ["first", "second"]
