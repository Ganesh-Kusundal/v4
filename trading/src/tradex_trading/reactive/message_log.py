"""Persistent JSONL-backed event store.

Writes domain events to a JSONL file (one JSON object per line) and
supports replay / replay_range for event-sourcing scenarios.
"""

from __future__ import annotations

import dataclasses
import json
from collections.abc import Iterator
from datetime import datetime
from pathlib import Path
from typing import Any


class _Encoder(json.JSONEncoder):
    """JSON encoder that handles datetime and Decimal."""

    def default(self, obj: Any) -> Any:
        if isinstance(obj, datetime):
            return obj.isoformat()
        try:
            from decimal import Decimal
            if isinstance(obj, Decimal):
                return str(obj)
        except ImportError:
            pass
        return super().default(obj)


def _serialize(message: Any) -> dict[str, Any]:
    """Convert a message to a dict for JSON serialization."""
    if isinstance(message, dict):
        return message
    if hasattr(message, "to_dict") and callable(message.to_dict):
        return message.to_dict()  # type: ignore[no-any-return]
    if dataclasses.is_dataclass(message):
        return dataclasses.asdict(message)  # type: ignore[arg-type]
    raise TypeError(f"Cannot serialize message of type {type(message).__name__}")


class FileMessageLog:
    """Persistent event store backed by a JSONL file.

    Each line in the file is a single JSON object representing one event.
    """

    def __init__(self, path: str | Path) -> None:
        self._path = Path(path)
        self._path.parent.mkdir(parents=True, exist_ok=True)
        if not self._path.exists():
            self._path.touch()

    # ------------------------------------------------------------------
    # Writing
    # ------------------------------------------------------------------

    def append(self, message: Any) -> None:
        """Serialize *message* and append it as a single JSON line."""
        data = _serialize(message)
        line = json.dumps(data, cls=_Encoder)
        with self._path.open("a", encoding="utf-8") as fh:
            fh.write(line + "\n")

    # ------------------------------------------------------------------
    # Reading
    # ------------------------------------------------------------------

    def replay(self) -> Iterator[dict[str, Any]]:
        """Yield every stored event as a dict."""
        if not self._path.exists():
            return
        with self._path.open("r", encoding="utf-8") as fh:
            for line in fh:
                stripped = line.strip()
                if stripped:
                    yield json.loads(stripped)

    def replay_range(
        self,
        start: datetime | None = None,
        end: datetime | None = None,
    ) -> Iterator[dict[str, Any]]:
        """Yield events whose ``timestamp`` falls within [start, end]."""
        for event in self.replay():
            ts_raw = event.get("timestamp")
            if ts_raw is None:
                continue
            if isinstance(ts_raw, str):
                ts = datetime.fromisoformat(ts_raw)
            else:
                ts = ts_raw  # type: ignore[assignment]
            if start is not None and ts < start:
                continue
            if end is not None and ts > end:
                continue
            yield event

    # ------------------------------------------------------------------
    # Introspection
    # ------------------------------------------------------------------

    @property
    def count(self) -> int:
        """Number of lines currently in the file."""
        if not self._path.exists():
            return 0
        total = 0
        with self._path.open("r", encoding="utf-8") as fh:
            for line in fh:
                if line.strip():
                    total += 1
        return total
