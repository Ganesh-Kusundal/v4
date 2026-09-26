"""Durable SQLite-backed event store for the OMS.

Implements the ``EventStore`` protocol from ``recovery.py`` with a persistent
append-only log.  All writes go through WAL mode so an unclean shutdown never
leaves a partial write visible to the next reader.

Schema
------
events(
    sequence        INTEGER PRIMARY KEY AUTOINCREMENT,
    stream_id       TEXT    NOT NULL,        -- "orders", "system", …
    event_type      TEXT    NOT NULL,        -- fully-qualified class name
    event_id        TEXT    NOT NULL UNIQUE, -- UUID4 minted on append
    correlation_id  TEXT,                    -- from DomainEvent.correlation_id
    payload         TEXT    NOT NULL,        -- pickle+base64
    created_at      TEXT    NOT NULL,        -- ISO-8601 UTC
    schema_version  INTEGER NOT NULL DEFAULT 1
)

Supported event types (SDD §2.2)
---------------------------------
OrderPlaced, OrderFilled, OrderRejected, OrderCancelled, OrderModified,
PlaceOrderCommand — plus any future DomainEvent subclass (open/closed OCP).

Thread safety
-------------
A threading.Lock guards every write; SQLite WAL mode gives concurrent reads.
"""

from __future__ import annotations

import base64
import logging
import pickle
import sqlite3
import threading
import uuid
from collections.abc import Iterator
from datetime import UTC, datetime
from pathlib import Path

from tradex_domain.events import DomainEvent

_log = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Event types we explicitly recognise and route to stream "orders".
# Any unknown DomainEvent subclass still goes into stream "system" so it is
# durable without crashing the append path.
# ---------------------------------------------------------------------------
_ORDER_EVENT_CLASSNAMES = frozenset({
    "OrderPlaced",
    "OrderFilled",
    "OrderRejected",
    "OrderCancelled",
    "OrderModified",
    "PlaceOrderCommand",
    # Wave C1 — full OMS event set
    "RiskDecision",
    "BrokerOrderRequested",
    "BrokerOrderAcknowledged",
    "CancelRequested",
    "UnknownSubmission",
    # Cash fold — the opening anchor and accepted corrections are part of the
    # order-scoped log a restart replays to rebuild the ledger.
    "CashAccountInitialized",
    "CashReconciled",
    # ReconciliationResult → "system" stream (not order-scoped)
})


def _infer_stream(event: DomainEvent) -> str:
    return "orders" if type(event).__name__ in _ORDER_EVENT_CLASSNAMES else "system"


#: Schema version this build reads and writes. Bump when an event's shape
#: changes, and add a decoder for every older version still in the wild.
SCHEMA_VERSION = 1


class EventDecodeError(RuntimeError):
    """A stored event could not be turned back into a domain event.

    The log is the sole authority for what happened, so a row that cannot be
    read means the reconstructed book would be missing real history. Skipping
    it and reporting success loses money quietly, which is worse than refusing
    to start.
    """


def _encode(event: DomainEvent) -> str:
    """Pickle the event and base64-encode it for TEXT storage."""
    return base64.b64encode(pickle.dumps(event)).decode("ascii")


def _decode(payload: str, schema_version: int = SCHEMA_VERSION) -> DomainEvent:
    """Reverse of ``_encode``, refusing anything this build cannot read.

    A version we have no decoder for is not decoded optimistically: the shape
    may differ in a way that silently produces wrong numbers.
    """
    if schema_version != SCHEMA_VERSION:
        raise EventDecodeError(
            f"event schema version {schema_version} cannot be read by this "
            f"build (understands {SCHEMA_VERSION})",
        )
    try:
        return pickle.loads(base64.b64decode(payload))  # noqa: S301 — local trusted store
    except Exception as exc:  # noqa: BLE001 — any decode failure is unrecoverable
        raise EventDecodeError(f"corrupt event payload: {exc}") from exc


class SQLiteEventStore:
    """Append-only, crash-safe event store backed by SQLite.

    Satisfies the ``EventStore`` protocol (``append`` + ``replay``).

    Parameters
    ----------
    db_path:
        File path for the SQLite database.  Pass ``":memory:"`` for an
        ephemeral in-process store (tests, single-run scripts).
    """

    _DDL = """
    CREATE TABLE IF NOT EXISTS events (
        sequence       INTEGER PRIMARY KEY AUTOINCREMENT,
        stream_id      TEXT    NOT NULL,
        event_type     TEXT    NOT NULL,
        event_id       TEXT    NOT NULL UNIQUE,
        correlation_id TEXT,
        payload        TEXT    NOT NULL,
        created_at     TEXT    NOT NULL,
        schema_version INTEGER NOT NULL DEFAULT 1
    );
    CREATE INDEX IF NOT EXISTS events_stream ON events (stream_id, sequence);
    """

    def __init__(self, db_path: str | Path = ":memory:") -> None:
        self._path = str(db_path)
        self._conn = sqlite3.connect(
            self._path,
            check_same_thread=False,
        )
        if db_path != ":memory:":
            # WAL survives unclean shutdown; busy_timeout avoids SQLITE_BUSY
            # under concurrent readers during live trading.
            self._conn.execute("PRAGMA journal_mode=WAL")
            self._conn.execute("PRAGMA busy_timeout=5000")
        self._conn.executescript(self._DDL)
        self._conn.commit()
        self._lock = threading.Lock()

    # ------------------------------------------------------------------
    # EventStore protocol
    # ------------------------------------------------------------------

    def append(self, event: DomainEvent, *, stream_id: str | None = None) -> int:
        """Persist *event* and return its monotonic sequence number.

        *stream_id* defaults to ``"orders"`` for all recognised order
        lifecycle event types, and ``"system"`` for anything else.
        Callers that route events to custom streams (e.g. ``"risk"``)
        may supply the override explicitly.

        The ``event_id`` is a fresh UUID4 minted on each append call —
        each logical append is a distinct row even if the same Python
        object is appended twice (the caller controls deduplication at
        a higher level via idempotency guards).
        """
        sid = stream_id if stream_id is not None else _infer_stream(event)
        event_type = type(event).__name__
        event_id = str(uuid.uuid4())
        corr = (
            str(event.correlation_id.value)
            if event.correlation_id is not None
            else None
        )
        created_at = datetime.now(UTC).isoformat()
        payload = _encode(event)

        with self._lock:
            cur = self._conn.execute(
                """INSERT INTO events
                   (stream_id, event_type, event_id, correlation_id, payload, created_at, schema_version)
                   VALUES (?, ?, ?, ?, ?, ?, ?)""",
                (sid, event_type, event_id, corr, payload, created_at, SCHEMA_VERSION),
            )
            self._conn.commit()
            return cur.lastrowid  # type: ignore[return-value]

    def replay(self, stream: str = "orders") -> Iterator[DomainEvent]:
        """Yield all events in *stream*, in sequence order.

        If *stream* is empty or ``"*"``, all streams are returned.

        Raises :class:`EventDecodeError` on the first row this build cannot
        read. A corrupt or unrecognised row is NOT skipped: the caller is
        rebuilding the authoritative book, and a silently dropped fill is a
        wrong balance rather than a missing one.
        """
        if stream and stream != "*":
            cursor = self._conn.execute(
                "SELECT payload, schema_version FROM events "
                "WHERE stream_id = ? ORDER BY sequence",
                (stream,),
            )
        else:
            cursor = self._conn.execute(
                "SELECT payload, schema_version FROM events ORDER BY sequence",
            )
        for payload, schema_version in cursor:
            yield _decode(payload, schema_version)

    # ------------------------------------------------------------------
    # Diagnostics
    # ------------------------------------------------------------------

    def count(self, stream: str = "orders") -> int:
        """Return the number of events in *stream* (or total if ``"*"``)."""
        if stream and stream != "*":
            row = self._conn.execute(
                "SELECT COUNT(*) FROM events WHERE stream_id = ?", (stream,)
            ).fetchone()
        else:
            row = self._conn.execute("SELECT COUNT(*) FROM events").fetchone()
        return row[0] if row else 0

    def last_sequence(self, stream: str = "orders") -> int:
        """Return the highest sequence number in *stream*, or 0 if empty."""
        if stream and stream != "*":
            row = self._conn.execute(
                "SELECT MAX(sequence) FROM events WHERE stream_id = ?", (stream,)
            ).fetchone()
        else:
            row = self._conn.execute("SELECT MAX(sequence) FROM events").fetchone()
        return row[0] if (row and row[0] is not None) else 0

    def close(self) -> None:
        """Close the underlying SQLite connection."""
        self._conn.close()


