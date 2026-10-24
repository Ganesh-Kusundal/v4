"""Replay of a ReactiveBus backed by an SQLEventLog."""

from __future__ import annotations

import tempfile
from pathlib import Path

from tradex_reactive.bus import ReactiveBus
from tradex_reactive.event_log import SQLEventLog


def test_replay_bus_with_sql_backed_log() -> None:
    with tempfile.TemporaryDirectory() as tmp:
        path = Path(tmp) / "bus_events.db"
        bus = ReactiveBus(event_log=SQLEventLog(path))
        try:
            bus.publish("x")
            bus.publish("y")
            replayed: list[str] = []
            bus.replay().subscribe(lambda m: replayed.append(m))
            assert replayed == ["x", "y"]
        finally:
            bus._log.close()  # noqa: SLF001