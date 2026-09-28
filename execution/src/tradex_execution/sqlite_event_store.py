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
    schema_version  INTEGER NOT NULL DEFAULT 1,
    prev_hash       TEXT,                    -- event_hash of the preceding row
    event_hash      TEXT                     -- hash of THIS row's content
)

Two durability properties hang off that table, and both are enforced on the
READ path, not merely recorded on write:

*Schema version.* Every row carries the ``schema_version`` it was written
under, and a decoder is REGISTERED per version (:func:`register_decoder`).
Bumping the current version does not invalidate history: an old row still
decodes through the decoder registered for its own version. A version with no
registered decoder is a hard failure (:class:`EventDecodeError`) — unknown
shape is never guessed at.

*Hash chain.* Each row stores the hash of its predecessor and the hash of its
own content. :meth:`SQLiteEventStore.replay` recomputes every hash and refuses
to yield a log that has been edited in place, reordered, or had an interior row
removed. A chain that is written but never verified on read is the same as no
chain at all, so verification is part of reading.

Supported event types (SDD §2.2)
---------------------------------
OrderPlaced, OrderFilled, OrderRejected, OrderCancelled, OrderModified,
PlaceOrderCommand — plus any future DomainEvent subclass (open/closed OCP).

PlaceOrderCommand is retained for READ compatibility only. AC-22 removed it
from the public bus, so no order can enter the pipeline through a published
command any more; the name survives here so a log written before that change
still decodes and files under the "orders" stream it was written to.

Thread safety
-------------
A threading.Lock guards every write; SQLite WAL mode gives concurrent reads.
"""

from __future__ import annotations

import base64
import hashlib
import logging
import pickle
import sqlite3
import threading
import uuid
from collections.abc import Callable, Iterator
from datetime import UTC, datetime
from pathlib import Path

from tradex_domain.events import DomainEvent

_log = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Event types we explicitly recognise and route to stream "orders".
# Any unknown DomainEvent subclass still goes into stream "system" so it is
# durable without crashing the append path.
#
# ``PlaceOrderCommand`` stays here even though AC-22 took it off the bus.
# This list is a STREAM-ROUTING table, not a list of things that may be
# published: it decides which durable log a decoded event belongs to, and the
# store decodes with ``pickle``. A log written before this change contains
# pickled PlaceOrderCommand rows, and dropping the name would silently
# re-route them to "system" on restart — the same order history, filed
# under a different stream than the run that wrote it. Keeping the name
# changes nothing about reachability: nothing constructs or subscribes to a
# command any more, so a new one can never be produced here.
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


#: Schema version this build WRITES. Bump when an event's shape changes — and,
#: in the same commit, keep a decoder registered for the version being retired
#: so history written under it keeps decoding. See :func:`register_decoder`.
SCHEMA_VERSION = 1

#: The ``prev_hash`` of the very first event in a log. Any other value at the
#: head of a chain means the earliest rows were removed, which is a legitimate
#: store operation (see :meth:`SQLiteEventStore._reseed_truncated_head`) and not
#: evidence of tampering.
GENESIS_HASH = "0" * 64


class EventDecodeError(RuntimeError):
    """A stored event could not be turned back into a domain event.

    The log is the sole authority for what happened, so a row that cannot be
    read means the reconstructed book would be missing real history. Skipping
    it and reporting success loses money quietly, which is worse than refusing
    to start.
    """


class EventChainError(EventDecodeError):
    """The stored hash chain does not describe the log it is attached to.

    Raised when a row's content no longer hashes to its recorded
    ``event_hash``, or when a row's ``prev_hash`` does not match the event
    immediately before it. Either means the log was edited in place, rows were
    reordered, or an interior row was removed. Recovery is rebuilding the
    authoritative book from this log, so a book that cannot be proven faithful
    is not a book to trade on.

    Subclasses :class:`EventDecodeError`: both mean the same thing to a caller
    rebuilding the book — the log cannot be read as written — and a corrupt
    payload, an unknown version, and a broken chain are all ways that happens.
    A caller that must distinguish "I have no decoder for this shape" from
    "someone edited the log" can still catch each class on its own.
    """


# ---------------------------------------------------------------------------
# Schema-version decoder registry
# ---------------------------------------------------------------------------
# A decoder maps a stored payload (plus the row's schema_version) back to a
# DomainEvent. The CURRENT version's decoder is the plain unpickle; older
# versions register their own so a bump never invalidates history. The registry
# is the mechanism that makes "old history still decodes" true by construction
# rather than by hope.
_DECODERS: dict[int, Callable[[bytes], DomainEvent]] = {}


def register_decoder(version: int, decoder: Callable[[bytes], DomainEvent]) -> None:
    """Register *decoder* as the reader for events stored under *version*.

    Call this for EVERY version that exists in any log the platform must still
    read, including the one being retired. Overwriting a version is allowed and
    is the intended way to correct a decoder.
    """
    _DECODERS[int(version)] = decoder


def registered_versions() -> tuple[int, ...]:
    """Return the schema versions this build can decode, ascending."""
    return tuple(sorted(_DECODERS))


def _unpickle(raw: bytes) -> DomainEvent:
    return pickle.loads(raw)  # noqa: S301 — local trusted store


#: Version 1 is the current format, so its decoder is the plain unpickle. It
#: is registered at import rather than referenced inline so that a future
#: version bump is a one-line change here PLUS a registration for the retired
#: version — the current one keeps its own entry in the registry forever.
register_decoder(SCHEMA_VERSION, _unpickle)


def _encode(event: DomainEvent) -> str:
    """Pickle the event and base64-encode it for TEXT storage."""
    return base64.b64encode(pickle.dumps(event)).decode("ascii")


def _decode(payload: str, schema_version: int = SCHEMA_VERSION) -> DomainEvent:
    """Reverse of ``_encode``, routed through the decoder for *schema_version*.

    A version with a registered decoder is decoded by it, so history written
    before a version bump keeps working. A version with NO registered decoder
    is a hard error: the shape is unknown, and guessing would silently produce
    wrong numbers.
    """
    decoder = _DECODERS.get(int(schema_version))
    if decoder is None:
        raise EventDecodeError(
            f"event schema version {schema_version} has no registered decoder "
            f"in this build (can read {registered_versions()})",
        )
    try:
        raw = base64.b64decode(payload)
    except Exception as exc:  # noqa: BLE001 — any decode failure is unrecoverable
        raise EventDecodeError(f"corrupt event payload: {exc}") from exc
    try:
        return decoder(raw)
    except EventDecodeError:
        raise
    except Exception as exc:  # noqa: BLE001 — any decode failure is unrecoverable
        raise EventDecodeError(
            f"corrupt event payload at schema version {schema_version}: {exc}",
        ) from exc


# ---------------------------------------------------------------------------
# Hash chain
# ---------------------------------------------------------------------------

def _row_hash(
    sequence: int,
    stream_id: str,
    event_type: str,
    event_id: str,
    correlation_id: str | None,
    payload: str,
    created_at: str,
    schema_version: int,
    prev_hash: str,
) -> str:
    """Hash one row's full, immutable content, bound to its predecessor.

    Every stored field participates, so editing ANY of them — payload, stream,
    type, correlation, timestamp, or the prev_hash link itself — changes the
    digest. Including ``prev_hash`` ties the row to its position in the chain,
    which is what makes reordering and interior deletion detectable.
    """
    parts = "\x1f".join((
        str(sequence), stream_id, event_type, event_id,
        correlation_id or "", payload, created_at,
        str(schema_version), prev_hash,
    ))
    return hashlib.sha256(parts.encode("utf-8")).hexdigest()


#: Columns fetched for verification, in a fixed order.
_CHAIN_COLUMNS = (
    "sequence, stream_id, event_type, event_id, correlation_id, "
    "payload, created_at, schema_version, prev_hash, event_hash"
)


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
        schema_version INTEGER NOT NULL DEFAULT 1,
        prev_hash      TEXT,
        event_hash     TEXT
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
        self._migrate_legacy_rows()

    # ------------------------------------------------------------------
    # Migration
    # ------------------------------------------------------------------

    def _columns(self) -> set[str]:
        return {
            row[1] for row in self._conn.execute("PRAGMA table_info(events)").fetchall()
        }

    def _migrate_legacy_rows(self) -> None:
        """Bring a pre-chain database up to the current schema, in place.

        A database written before this lane carries no ``prev_hash`` or
        ``event_hash``. Rather than require a fresh file — which would strand
        every existing log and silently lose the history it holds — the columns
        are added and the existing rows are BACKFILLED: each legacy row is
        hashed exactly as it would have been at write time, chained to its
        predecessor. The rows themselves are never rewritten, reordered, or
        dropped, so history written by an earlier build is preserved verbatim
        and becomes chain-verifiable from here on.
        """
        columns = self._columns()
        if "prev_hash" in columns and "event_hash" in columns:
            return

        with self._lock:
            for column in ("prev_hash", "event_hash"):
                if column not in self._columns():
                    self._conn.execute(f"ALTER TABLE events ADD COLUMN {column} TEXT")
            rows = self._conn.execute(
                "SELECT sequence, stream_id, event_type, event_id, correlation_id, "
                "payload, created_at, schema_version, prev_hash, event_hash "
                "FROM events ORDER BY sequence",
            ).fetchall()
            previous = GENESIS_HASH
            for (
                sequence, stream_id, event_type, event_id, correlation_id,
                payload, created_at, schema_version, prev_hash, event_hash,
            ) in rows:
                # A row already carrying a hash in the right place is left
                # exactly as it is: it was written by a chain-aware build and its
                # value is authoritative. This is what makes a re-open (or a
                # retry after a crash mid-migration) idempotent.
                if event_hash is not None and prev_hash == previous:
                    previous = event_hash
                    continue
                # An un-chained row (legacy, or stranded by an interrupted
                # migration) is hashed now and linked to the row before it. The
                # very first row of the log links to genesis.
                prev_value = prev_hash if prev_hash is not None else previous
                digest = _row_hash(
                    sequence, stream_id, event_type, event_id, correlation_id,
                    payload, created_at, schema_version, prev_value,
                )
                self._conn.execute(
                    "UPDATE events SET prev_hash = ?, event_hash = ? WHERE sequence = ?",
                    (prev_value, digest, sequence),
                )
                previous = digest
            self._conn.commit()

    # ------------------------------------------------------------------
    # EventStore protocol
    # ------------------------------------------------------------------

    def append(self, event: DomainEvent, *, stream_id: str | None = None) -> int:
        """Persist *event* and return its monotonic sequence number.

        *stream_id* defaults to ``"orders"`` for all recognised order
        lifecycle event types, and ``"system"`` for anything else.
        Callers that route events to custom streams (e.g. ``"risk"``)
        may supply the override explicitly.

        The row is written with ``prev_hash`` bound to the current chain head,
        so the log is tamper-evident the moment it lands rather than only
        after a later audit.

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
            # The sequence is assigned by SQLite, but the hash covers it, so
            # the row is inserted first and hashed immediately after, in the
            # same transaction — a reader never sees an unhashed row.
            cur = self._conn.execute(
                """INSERT INTO events
                   (stream_id, event_type, event_id, correlation_id,
                    payload, created_at, schema_version, prev_hash, event_hash)
                   VALUES (?, ?, ?, ?, ?, ?, ?, NULL, NULL)""",
                (sid, event_type, event_id, corr, payload, created_at, SCHEMA_VERSION),
            )
            sequence = cur.lastrowid
            head = self._conn.execute(
                "SELECT event_hash FROM events WHERE sequence < ? "
                "ORDER BY sequence DESC LIMIT 1",
                (sequence,),
            ).fetchone()
            prev_hash = head[0] if head and head[0] is not None else GENESIS_HASH
            digest = _row_hash(
                sequence, sid, event_type, event_id, corr,
                payload, created_at, SCHEMA_VERSION, prev_hash,
            )
            self._conn.execute(
                "UPDATE events SET prev_hash = ?, event_hash = ? WHERE sequence = ?",
                (prev_hash, digest, sequence),
            )
            self._conn.commit()
            return sequence  # type: ignore[return-value]

    def _verify_chain(
        self,
        stream: str = "*",
    ) -> Iterator[tuple[int, str, int]]:
        """Yield ``(sequence, payload, schema_version)`` after verifying the chain.

        The chain is a single, global, sequence-ordered chain over EVERY row in
        the log, not one chain per stream. Verification therefore always walks
        the whole log in ``sequence`` order — otherwise a per-stream replay
        that starts mid-chain (the first ``"system"`` row is rarely sequence 1)
        would compare that row's ``prev_hash`` against genesis and report a
        break that never happened. When *stream* names one stream, the rows it
        filters out are still walked to carry the chain forward; only the
        yielded tuples are filtered.

        Every row is re-hashed from its own stored fields and compared to the
        recorded ``event_hash``, and every row's ``prev_hash`` is compared to
        the ``event_hash`` of the row immediately before it. The FIRST row seeds
        the chain from its OWN recorded ``prev_hash``: on an intact log that is
        :data:`GENESIS_HASH`, but after a legitimate head truncation (an
        operator dropping a bad opening anchor) the first surviving row points
        at a predecessor that is simply gone, and refusing to read would reject
        surgery the store already treats as legitimate. Every link after the
        first, and every row's own content, is checked strictly.

        This runs on the READ path. Recording a chain that nothing ever checks
        would leave the store exactly as tamperable as it was before, so an
        inconsistency raises :class:`EventChainError` here rather than being
        logged and passed over.
        """
        cursor = self._conn.execute(
            f"SELECT {_CHAIN_COLUMNS} FROM events ORDER BY sequence",
        )

        want_all = (not stream) or stream == "*"
        previous_hash: str | None = None  # None until the first row is seen
        for row in cursor:
            (
                sequence, stream_id, event_type, event_id, correlation_id,
                payload, created_at, schema_version, prev_hash, event_hash,
            ) = row
            if event_hash is None or prev_hash is None:
                raise EventChainError(
                    f"event {sequence} carries no hash; the log was not "
                    f"written by a chain-aware build",
                )
            if previous_hash is None:
                # First row: accept its recorded link (genesis, or a dangling
                # predecessor after a legitimate head truncation).
                previous_hash = prev_hash
            elif prev_hash != previous_hash:
                raise EventChainError(
                    f"event {sequence} (type {event_type}) records "
                    f"prev_hash {str(prev_hash)[:12]}... but the event before "
                    f"it hashes to {str(previous_hash)[:12]}...: the log was "
                    f"edited, reordered, or an interior row was removed",
                )
            recomputed = _row_hash(
                sequence, stream_id, event_type, event_id, correlation_id,
                payload, created_at, schema_version, prev_hash,
            )
            if recomputed != event_hash:
                raise EventChainError(
                    f"event {sequence} (type {event_type}) content does not "
                    f"match its recorded hash: the row was modified in place",
                )
            previous_hash = event_hash
            if want_all or stream_id == stream:
                yield sequence, payload, schema_version

    def replay(self, stream: str = "orders") -> Iterator[DomainEvent]:
        """Yield all events in *stream*, in sequence order.

        If *stream* is empty or ``"*"``, all streams are returned.

        The hash chain is verified as the log is read, so a row that has been
        edited, reordered, or removed from the middle stops the replay with
        :class:`EventChainError`. A rebuild from a log that cannot be proven
        faithful is not a rebuild worth trusting.

        Raises :class:`EventDecodeError` on the first row this build cannot
        read. A corrupt or unrecognised row is NOT skipped: the caller is
        rebuilding the authoritative book, and a silently dropped fill is a
        wrong balance rather than a missing one.
        """
        for _sequence, payload, schema_version in self._verify_chain(stream):
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


