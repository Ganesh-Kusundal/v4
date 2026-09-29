"""Durable SQLite-backed event store for the OMS.

Implements the ``EventStore`` protocol from ``recovery.py`` with a persistent
append-only log.  All writes go through WAL mode so an unclean shutdown never
leaves a partial write visible to the next reader.

Schema
------
events(
    sequence        INTEGER PRIMARY KEY AUTOINCREMENT,
    stream_id       TEXT    NOT NULL,        -- "<broker>:<account>:<stream>"
    event_type      TEXT    NOT NULL,        -- fully-qualified class name
    event_id        TEXT    NOT NULL UNIQUE, -- UUID4 minted on append
    correlation_id  TEXT,                    -- from DomainEvent.correlation_id
    payload         TEXT    NOT NULL,        -- pickle+base64
    created_at      TEXT    NOT NULL,        -- ISO-8601 UTC
    schema_version  INTEGER NOT NULL DEFAULT 1,
    prev_hash       TEXT,                    -- event_hash of the preceding row
    event_hash      TEXT                     -- hash of THIS row's content
)

snapshots(
    stream_id        TEXT    NOT NULL,  -- the account stream the snapshot folds
    through_sequence INTEGER NOT NULL,  -- highest sequence folded into it
    payload          TEXT    NOT NULL,  -- canonical JSON (never pickle)
    created_at       TEXT    NOT NULL,
    reason           TEXT    NOT NULL,  -- "ready" | "periodic"
    PRIMARY KEY (stream_id, through_sequence)
)

Three durability properties hang off those tables, and all of them are
enforced on the READ path, not merely recorded on write:

*Account-scoped streams (version 2).* Rows are written to
``f"{broker_id}:{account_id}:{stream}"`` so two brokers — or two accounts at
one broker — never share a log partition. The store takes ``broker_id`` and
``account_id`` at construction; the default ``"*"`` keeps the un-scoped stream
names of the pre-2.0 schema, which is what every existing caller and every
existing database still holds.

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

The version-1 -> version-2 bump is the hard case, and it is why the decoder
registry exists. Version 2 changed the *stored content* of ``stream_id``, so a
v1 row and a v2 row that describe the same order hash to different digests.
Rather than rewrite history (which is exactly what the chain exists to detect)
the store keeps both hash functions: a v1 row is verified with the v1 hash over
its own stored fields, a v2 row with the account-scoped hash. Old history still
replays with verification passing, and new rows are account-scoped.

Snapshots bound the replay cost: recovery reads the newest snapshot for its
account stream and replays strictly after it. A snapshot is written when the
session reaches READY and every :data:`SNAPSHOT_INTERVAL` events, and the log
below a snapshot's ``through_sequence`` is never read except by that snapshot,
with the chain still verified across the boundary.

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
import io
import json
import logging
import pickle
import sqlite3
import threading
import uuid
from collections.abc import Callable, Iterator
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path

from tradex_domain.events import DomainEvent

from tradex_execution._sqlite import configure_sqlite

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
#:
#: Version 2 changed the *content* of ``stream_id`` from a bare stream name to
#: an account-scoped ``"{broker}:{account}:{stream}"``. That changes every
#: row's hash, so the hash function is version-keyed too (see
#: :func:`_row_hash`) and the v1 decoder registered below keeps the v1 rows
#: verifiable as written.
SCHEMA_VERSION = 2

#: The ``prev_hash`` of the very first event in a log. Any other value at the
#: head of a chain means the earliest rows were removed, which is a legitimate
#: store operation (see :meth:`SQLiteEventStore._reseed_truncated_head`) and not
#: evidence of tampering.
GENESIS_HASH = "0" * 64

#: The un-scoped stream placeholder used when a caller does not name a broker
#: or account. It is deliberately NOT a real account id: an un-scoped store
#: keeps the bare stream names of the pre-2.0 schema, which is the shape every
#: existing database on disk still holds.
UNSCOPED_ACCOUNT = "*"

#: How many appended events elapse between periodic snapshots. The spec sets
#: this at 10,000; a snapshot is also written once per stream when the session
#: reaches READY (see :meth:`SQLiteEventStore.write_snapshot`).
SNAPSHOT_INTERVAL = 10_000


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


class UnpickleSecurityError(EventDecodeError):
    """A stored payload named a class this build refuses to reconstruct.

    Raised by the restricted :class:`_AllowlistUnpickler` the moment a pickle
    asks for a global that is not in :data:`ALLOWED_PICKLE_CLASSES`. It is a
    distinct class rather than a message inside :class:`EventDecodeError`
    because it is the one decode failure that means something adversarial
    rather than something damaged: an unknown class in an otherwise intact
    log is a payload that was written to run code, and a caller that wants to
    count those separately from a corrupt row needs them apart.

    It subclasses :class:`EventDecodeError` so every existing caller that
    already handles "this row cannot be read" keeps handling it — refusing to
    start is the correct behaviour for both.
    """


@dataclass(frozen=True, slots=True)
class Snapshot:
    """A folded view of one account stream at a sequence number.

    ``payload`` is canonical JSON, never pickle: a snapshot is a recovery
    artefact that a later build must be able to read without reconstructing
    the exact object graph that wrote it, and a pickle would couple the
    snapshot's readability to the class layout of the build that produced it.

    ``through_sequence`` is the highest event sequence folded into the
    snapshot, and replay starts strictly AFTER it. The log below it is never
    read except by the snapshot itself, and the chain is still verified across
    the boundary.
    """

    stream_id: str
    through_sequence: int
    payload: dict
    created_at: str
    reason: str = "periodic"

    @staticmethod
    def serialise(state: object) -> str:
        """Canonical JSON for *state*, so equal states always serialise equal.

        ``sort_keys`` and no cosmetic whitespace make the digest of a snapshot
        byte-stable, which is what a test or an audit needs to compare two
        snapshots for equality rather than two serialisations for style.
        """
        return json.dumps(state, sort_keys=True, separators=(",", ":"))

    @staticmethod
    def deserialise(text: str) -> dict:
        return json.loads(text)

    def payload_json(self) -> str:
        return Snapshot.serialise(self.payload)

    @staticmethod
    def from_row(stream_id: str, through_sequence: int, payload: str,
                 created_at: str, reason: str) -> Snapshot:
        return Snapshot(
            stream_id=stream_id,
            through_sequence=int(through_sequence),
            payload=Snapshot.deserialise(payload),
            created_at=created_at,
            reason=reason,
        )

    @staticmethod
    def empty(stream_id: str, sequence: int, reason: str = "ready") -> Snapshot:
        """The READY snapshot for a stream with no folded state of its own.

        ``through_sequence`` still names the sequence the snapshot was taken
        after, so replay starts strictly after it and cannot re-read the rows
        the snapshot already folded.
        """
        return Snapshot(
            stream_id=stream_id,
            through_sequence=int(sequence),
            payload={},
            created_at=datetime.now(UTC).isoformat(),
            reason=reason,
        )


# ---------------------------------------------------------------------------
# Account-scoped streams
# ---------------------------------------------------------------------------
def scoped_stream(
    broker_id: str | None, account_id: str | None, stream: str,
) -> str:
    """Compose the account-scoped stream id: ``"{broker}:{account}:{stream}"``.

    ``"*"`` is the explicit un-scoped placeholder: it keeps the bare stream
    name, which is the shape a pre-2.0 database holds, so a store opened
    without account context reads and appends exactly the stream ids already
    on disk. A ``None`` id means the same thing — it is how an un-scoped store
    describes its own scope — so both spellings must resolve identically, or a
    store constructed with ``None`` would scope its rows while a store
    constructed with ``"*"`` would not.

    The two guards are separate on purpose: a broker is a process-level fact
    and an account is a session-level one, and either being absent is enough to
    leave the stream bare. A single ``and`` between them would still do that,
    but it would lose the reason in the stack trace when someone asks why an
    un-scoped open produced bare names.
    """
    if not broker_id or broker_id == UNSCOPED_ACCOUNT:
        return stream
    if not account_id or account_id == UNSCOPED_ACCOUNT:
        return stream
    if not stream:
        return stream
    return f"{broker_id}:{account_id}:{stream}"


def stream_suffix(stream_id: str) -> str:
    """Return the bare stream name (``"orders"``) of any stream id.

    Works for account-scoped and legacy bare stream ids alike, so the routing
    table and a caller that asked for a stream by its old name both resolve.
    """
    if not stream_id:
        return stream_id
    # Take the last colon-separated segment only: a bare stream id never
    # contains a colon, and a scoped one always ends with the bare name.
    return stream_id.rsplit(":", 2)[-1]


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

    A registration is also what makes the version's rows hashable for chain
    verification (see :func:`_hash_versions`), so a version a build cannot
    decode is a version whose rows it does not pretend to verify either. Mark
    a version account-scoped with :func:`mark_scoped_version`.
    """
    _DECODERS[int(version)] = decoder


def mark_scoped_version(version: int) -> None:
    """Declare that *version* stores account-scoped stream ids.

    Version 2 is the version that moved ``stream_id`` from a bare stream name
    to ``"{broker}:{account}:{stream}"``. Hashing a v1 row with the v2 scheme
    would fail an honest row, so the scoped set — not the version number's
    magnitude — decides which digest applies. Register the version first.
    """
    int_version = int(version)
    if int_version not in _DECODERS:
        raise EventDecodeError(
            f"cannot mark version {int_version} account-scoped: no decoder is "
            f"registered for it (registered: {registered_versions()})",
        )
    _SCOPED_VERSIONS.add(int_version)


def registered_versions() -> tuple[int, ...]:
    """Return the schema versions this build can decode, ascending."""
    return tuple(sorted(_DECODERS))


# ---------------------------------------------------------------------------
# Restricted unpickling (C3)
# ---------------------------------------------------------------------------
# The payload column is pickle, and pickle is not a data format — it is a
# small interpreter that will call whatever a payload names. Anyone who can
# write a row therefore had arbitrary code execution on the next replay() the
# moment this store was opened by anything.
#
# The hash chain does not change that. :func:`_hash_input` is unkeyed SHA-256
# over the row's own fields, so a writer who can insert a row can also
# recompute that row's digest and the next one's prev_hash: the chain proves
# the log was not edited AFTER it was written, which is not the threat here.
# The threat is the writer, and the chain is not a defence against the writer.
#
# So decoding goes through an Unpickler whose find_class consults an EXPLICIT
# set of ``(module, qualname)`` pairs. An unlisted name raises rather than
# being resolved, and there is deliberately no fallback to unrestricted
# unpickling: a fallback would make the allowlist a speed bump that any
# payload could switch off by naming one unusual class.
#
# The set below is the transitive closure of what a legitimately written event
# can contain — not just the event class, but everything pickle reaches on the
# way down (the value objects, enums, and instrument dataclasses that ride
# inside an Order, a Fill, a Position, or a Quote). It was derived
# EMPIRICALLY: every class in :mod:`tradex_domain.events` was constructed with
# representative field values and the globals its pickle actually names were
# recorded. `test_the_allowlist_covers_every_domain_event` re-derives that
# closure and fails if a future event type introduces a class this list does
# not carry, so the list cannot silently fall behind the domain.
#
# Membership is by exact (module, qualname), never by prefix: a prefix rule
# would re-admit `tradex_domain.events.<anything>`, which is the whole
# surface. "Any DomainEvent subclass" is the OCP property the module docstring
# promises, and it is preserved — but OCP now costs one line in a frozenset,
# and an unreviewed line in a list cannot run code.
ALLOWED_PICKLE_CLASSES: frozenset[tuple[str, str]] = frozenset({
    # -- standard library value types the domain stores ------------------------
    # datetime/timedelta/timezone: DomainEvent.timestamp is an aware datetime
    # and its tzinfo rides along by name. date: Instrument.expiry for
    # futures and options. ValueError: the type carried by ErrorOccurred.
    # Nothing here is a namespace or a callable with side effects — these are
    # all leaf constructors that either return an argument or raise.
    ("builtins", "ValueError"),
    ("datetime", "date"),
    ("datetime", "datetime"),
    ("datetime", "timedelta"),
    ("datetime", "timezone"),
    ("decimal", "Decimal"),

    # -- tradex_domain.enums ---------------------------------------------------
    # Every enum reachable from an event: order lifecycle fields on Order and
    # OrderRequest, and exchange/asset-class on any Instrument.
    ("tradex_domain.enums", "AssetClass"),
    ("tradex_domain.enums", "ExchangeId"),
    ("tradex_domain.enums", "OrderSide"),
    ("tradex_domain.enums", "OrderStatus"),
    ("tradex_domain.enums", "OrderType"),
    ("tradex_domain.enums", "ProductType"),
    ("tradex_domain.enums", "TimeInForce"),

    # -- tradex_domain.events --------------------------------------------------
    # ALL twenty DomainEvent types, not just the ones the current OMS emits.
    # The log outlives the build that wrote it, so a future event type will
    # arrive here first as a row an older build must still read; refusing to
    # decode it would make a schema addition look like data loss.
    ("tradex_domain.events", "BrokerOrderAcknowledged"),
    ("tradex_domain.events", "BrokerOrderRequested"),
    ("tradex_domain.events", "CancelRequested"),
    ("tradex_domain.events", "CashAccountInitialized"),
    ("tradex_domain.events", "CashReconciled"),
    ("tradex_domain.events", "DomainEvent"),
    ("tradex_domain.events", "ErrorOccurred"),
    ("tradex_domain.events", "FeedGapDetected"),
    ("tradex_domain.events", "FeedRecovered"),
    ("tradex_domain.events", "OrderCancelled"),
    ("tradex_domain.events", "OrderFilled"),
    ("tradex_domain.events", "OrderModified"),
    ("tradex_domain.events", "OrderPlaced"),
    ("tradex_domain.events", "OrderRejected"),
    ("tradex_domain.events", "PlaceOrderCommand"),
    ("tradex_domain.events", "PositionUpdated"),
    ("tradex_domain.events", "ReconciliationResult"),
    ("tradex_domain.events", "RiskDecision"),
    ("tradex_domain.events", "StaleFeed"),
    ("tradex_domain.events", "UnknownSubmission"),

    # -- tradex_domain.execution ----------------------------------------------
    # Order: the payload of the whole order lifecycle. OrderRequest: the
    # payload of the request-intent events (RiskDecision, PlaceOrderCommand,
    # BrokerOrderRequested, UnknownSubmission). Fill / Position: OrderFilled
    # and PositionUpdated.
    ("tradex_domain.execution", "Fill"),
    ("tradex_domain.execution", "Order"),
    ("tradex_domain.execution", "OrderRequest"),
    ("tradex_domain.execution", "Position"),

    # -- tradex_domain.instruments --------------------------------------------
    # The base plus every concrete subclass, because an Order's instrument is
    # the concrete type that was constructed — pickling stores what the
    # instance is, not what the field's annotation permits. Admitting only
    # Equity would make a single futures order unreadable.
    ("tradex_domain.instruments", "Commodity"),
    ("tradex_domain.instruments", "Currency"),
    ("tradex_domain.instruments", "Equity"),
    ("tradex_domain.instruments", "Future"),
    ("tradex_domain.instruments", "Index"),
    ("tradex_domain.instruments", "Instrument"),
    ("tradex_domain.instruments", "InstrumentMeta"),
    ("tradex_domain.instruments", "Option"),

    # -- tradex_domain.market --------------------------------------------------
    # Quote: the optional second payload of PositionUpdated.
    ("tradex_domain.market", "Quote"),

    # -- tradex_domain.value_objects -------------------------------------------
    # The scalar wrappers every instrument and order field is built from.
    ("tradex_domain.value_objects", "CorrelationId"),
    ("tradex_domain.value_objects", "InstrumentId"),
    ("tradex_domain.value_objects", "OrderId"),
    ("tradex_domain.value_objects", "Price"),
    ("tradex_domain.value_objects", "Quantity"),
})


class _AllowlistUnpickler(pickle.Unpickler):
    """An ``Unpickler`` that resolves only an explicit set of classes.

    ``find_class`` is the single choke point: every ``GLOBAL`` / ``STACK_GLOBAL``
    opcode in a pickle — including the one buried inside a ``REDUCE`` that an
    attacker uses to call ``os.system`` or ``builtins.exec`` — resolves through
    it. Refusing here means the callable is never returned, so the ``REDUCE``
    that would have invoked it never runs and no argument is ever evaluated.

    The refusal is raised rather than logged so it propagates: a decode that
    could not be completed must not yield a partial event, because
    :meth:`SQLiteEventStore.replay` rebuilds the trading book from what it
    yields and a silently missing fill is a wrong balance rather than a
    missing one.
    """

    def __init__(self, file: io.BufferedIOBase, allowed: frozenset[tuple[str, str]]) -> None:
        super().__init__(file)
        self._allowed = allowed

    def find_class(self, module: str, name: str) -> type:
        key = (module, name)
        if key not in self._allowed:
            raise UnpickleSecurityError(
                f"refusing to unpickle {module}.{name}: it is not in the "
                f"allowlist of classes this build will reconstruct. A stored "
                f"payload may only name a domain event, a domain value object, "
                f"or a standard-library leaf type — anything else means the "
                f"log contains a payload that was written to execute code."
            )
        return super().find_class(module, name)


def _restricted_unpickle(raw: bytes, allowed: frozenset[tuple[str, str]]) -> object:
    """Unpickle *raw*, resolving nothing outside *allowed*.

    The shared entry point for every pickle sink in the persistence layer, so
    there is ONE place where a stored payload decides what may be constructed.
    ``sqlite_store`` calls it with a narrower set for its idempotency results
    rather than defining a second unpickler of its own.
    """
    return _AllowlistUnpickler(io.BytesIO(raw), allowed).load()


def _unpickle(raw: bytes) -> DomainEvent:
    """Decode a stored event payload through :data:`ALLOWED_PICKLE_CLASSES`.

    ``pickle.loads`` resolves whatever the payload names, so this function used
    to be an arbitrary-code-execution primitive pointed at a file on disk. The
    restricted unpickler in its place fails closed on an unlisted class.
    """
    return _restricted_unpickle(raw, ALLOWED_PICKLE_CLASSES)  # type: ignore[return-value]


#: The schema versions this build can VERIFY. A registered decoder implies a
#: hash function, so the set is DERIVED from the decoder registry rather than
#: maintained beside it: anything that adds or retires a decoder (including a
#: test simulating a version bump) keeps the two in step automatically. A
#: version with no decoder has a shape this build does not carry, and its rows
#: are yielded UNVERIFIED by :meth:`SQLiteEventStore._verify_chain` for
#: :func:`_decode` to refuse — an unknown version still fails, with a message
#: about decoders rather than a fabricated verdict about a hash that was
#: never computed.
def _hash_versions() -> set[int]:
    """The versions whose rows this build can hash."""
    return set(_DECODERS)


#: Versions whose rows hold an account-scoped stream_id. Everything below it
#: stored a bare stream name, and its rows must be hashed as written.
_SCOPED_VERSIONS: set[int] = set()


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

    The hash is VERSION-KEYED on ``stream_id``, because version 2 changed what
    a stream id *is*: a v1 ``"orders"`` row and a v2 ``"PAPER:*:orders"`` row
    for the same event are both honest, and hashing a v1 row with the scoped
    scheme would fail a row that was never written that way. A row of a version
    this build carries no hash function for never reaches here:
    :meth:`SQLiteEventStore._verify_chain` gates on :func:`_hash_versions`
    first and yields such a row unhashed, for :func:`_decode` to refuse with a
    message about the missing decoder rather than a fabricated verdict about a
    digest that was never computed.
    """
    if int(schema_version) in _SCOPED_VERSIONS:
        return _row_hash_scoped(
            sequence, stream_id, event_type, event_id, correlation_id,
            payload, created_at, schema_version, prev_hash,
        )
    return _row_hash_v1(
        sequence, stream_id, event_type, event_id, correlation_id,
        payload, created_at, schema_version, prev_hash,
    )


def _hash_input(
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
    parts = "\x1f".join((
        str(sequence), stream_id, event_type, event_id,
        correlation_id or "", payload, created_at,
        str(schema_version), prev_hash,
    ))
    return hashlib.sha256(parts.encode("utf-8")).hexdigest()


def _row_hash_scoped(
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
    """Version 2+ hash: the account scope is part of the row's identity.

    Two brokers writing the same event to ``"orders"`` must not be
    indistinguishable, so the scoped id — not the bare stream name — goes into
    the digest.
    """
    return _hash_input(
        sequence, stream_id, event_type, event_id, correlation_id,
        payload, created_at, schema_version, prev_hash,
    )


def _row_hash_v1(
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
    """Version 1 hash: over the row's stored fields exactly as written.

    Version 1 rows hold a bare stream name (``"orders"``), and that is what
    they were hashed over at write time. Re-hashing them with a later scheme
    would fail an honest row, which is the very history-preservation property
    the chain is for. This is the load-bearing counterpart of the v1 decoder.
    """
    return _hash_input(
        sequence, stream_id, event_type, event_id, correlation_id,
        payload, created_at, schema_version, prev_hash,
    )


# ---------------------------------------------------------------------------
# Account-scoped fill dedup
# ---------------------------------------------------------------------------
# Lane 4c item 5: the dedup key is ``(account, fingerprint)``, not the
# fingerprint alone. Two brokers in one process publish fills with overlapping
# venue trade ids and overlapping (order, side, qty, price) fallback tuples —
# one broker's fill would evict or suppress the other's. Partitioning by
# account keeps each account's index independent, and a ``None`` account
# degrades to the legacy global key so an un-scoped caller keeps working.


def dedup_key(account_id: str | None, fingerprint: tuple) -> tuple:
    """The fill dedup key, partitioned by account.

    ``account_id`` is the account the fill belongs to (the same account that
    scopes the stream the fill's event is appended to). ``fingerprint`` is the
    fill's own identity — venue trade id when the broker gives one, else the
    (order, side, quantity, price) tuple. The pair is the key: the same
    fingerprint under two accounts is two different executions, and the same
    account with two fingerprints is never a duplicate.
    """
    return (account_id or UNSCOPED_ACCOUNT, tuple(fingerprint))


def fill_fingerprint(fill: object) -> tuple:
    """Stable identity for a fill, independent of the account partition.

    Matches :meth:`tradex_execution.idempotency.FillDedup.fingerprint`, so a
    key computed at apply time is the key replay looks for, and the two
    implementations cannot drift.
    """
    fill_id = getattr(fill, "fill_id", None)
    if fill_id is not None:
        return (fill_id,)
    return (
        fill.order_id.value,          # type: ignore[union-attr]
        fill.side.value,              # type: ignore[union-attr]
        str(fill.quantity.value),     # type: ignore[union-attr]
        str(fill.price.value),        # type: ignore[union-attr]
    )


#: The partition an un-scoped caller lands in. It is a fixed name rather than
#: a derivation of :data:`UNSCOPED_ACCOUNT`, because ``*`` is not a usable
#: table identifier and the global partition predates account scoping.
_DEDUP_GLOBAL_TABLE = "fill_dedup_global"


def dedup_table_name(account_id: str | None) -> str:
    """The dedup partition name for *account_id*, injection-safe.

    Each account gets its own table, which is the strongest form of the
    partition: rows are physically unable to collide, and a bounded index
    stays bounded per account rather than across the process. An un-scoped
    caller lands in the ``fill_dedup_global`` table and keeps the old global
    semantics.

    Account ids are not restricted to letters and digits — ``acc-1`` is a
    legitimate id, and the same id appears unchanged in a scoped stream name.
    The name is used as a QUOTED identifier everywhere it appears, so the only
    shapes that can break out of it are a double quote and a NUL byte, and
    those are the two the check rejects.
    """
    if account_id is None:
        return _DEDUP_GLOBAL_TABLE
    if not _is_safe_identifier(account_id):
        raise ValueError(
            f"account_id is not usable as a dedup table name, got {account_id!r}",
        )
    return f"fill_dedup_{account_id}"


def _is_safe_identifier(text: str) -> bool:
    """Whether *text* can name a SQLite object inside double quotes.

    A quoted identifier may contain almost anything, but a double quote is
    the one character that can terminate it and a NUL byte is not legal in a
    SQLite string at all. Rejecting those two is the whole check.
    """
    return bool(text) and '"' not in text and "\x00" not in text


#: Columns of one account-partitioned dedup table. The composite key is the
#: whole point of item 5: ``(account, fingerprint)`` is unique, never the
#: fingerprint alone.
_DEDUP_COLUMNS = "fingerprint TEXT, seen_at TEXT"


#: Version 1 is the retired format: bare stream names and no account columns.
#: Its decoder is the plain unpickle, and its rows are verified with the v1
#: hash function. Registered at import, and its entry is kept forever — a
#: version bump retires a version, it does not delete it.
register_decoder(1, _unpickle)

#: Version 2 is the current format: account-scoped stream ids. Its decoder is
#: the same pickle (the payload itself is unchanged — only ``stream_id``
#: moved), but its rows are hashed over the scoped id, so the version is also
#: marked scoped. A v1 row and a v2 row for the same order therefore hash to
#: different digests, and both are honest — which is exactly why the hash
#: function is keyed on the version rather than applied uniformly.
register_decoder(SCHEMA_VERSION, _unpickle)
mark_scoped_version(SCHEMA_VERSION)


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


class SQLiteEventStore:
    """Append-only, crash-safe event store backed by SQLite.

    Satisfies the ``EventStore`` protocol (``append`` + ``replay``).

    Parameters
    ----------
    db_path:
        File path for the SQLite database.  Pass ``":memory:"`` for an
        ephemeral in-process store (tests, single-run scripts).
    broker_id, account_id:
        The account scope every appended event is filed under, as
        ``f"{broker_id}:{account_id}:{stream}"``. Defaults keep the bare
        stream names of the pre-2.0 schema, which is what an existing database
        on disk already holds — so an un-scoped open reads its own history
        without a rewrite, and a scoped open partitions new rows.
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

    CREATE TABLE IF NOT EXISTS snapshots (
        stream_id       TEXT    NOT NULL,
        through_sequence INTEGER NOT NULL,
        payload         TEXT    NOT NULL,
        created_at      TEXT    NOT NULL,
        reason          TEXT    NOT NULL DEFAULT 'periodic',
        PRIMARY KEY (stream_id, through_sequence)
    );
    CREATE INDEX IF NOT EXISTS snapshots_stream
        ON snapshots (stream_id, through_sequence);
    """

    def __init__(
        self,
        db_path: str | Path = ":memory:",
        *,
        broker_id: str = UNSCOPED_ACCOUNT,
        account_id: str = UNSCOPED_ACCOUNT,
    ) -> None:
        self._path = str(db_path)
        self.broker_id = broker_id or UNSCOPED_ACCOUNT
        self.account_id = account_id or UNSCOPED_ACCOUNT
        self._conn = sqlite3.connect(
            self._path,
            check_same_thread=False,
        )
        configure_sqlite(self._conn)
        self._conn.executescript(self._DDL)
        self._conn.commit()
        self._lock = threading.Lock()
        self._events_since_snapshot = self._events_since_last_snapshot()
        self._dedup_tables: set[str] = set()
        self._migrate_legacy_rows()

    # ------------------------------------------------------------------
    # Account scope
    # ------------------------------------------------------------------

    def _scope(self, stream: str) -> str:
        """The account-scoped stream id for *stream*, per this store's scope."""
        return scoped_stream(self.broker_id, self.account_id, stream)

    def _matches_stream(self, stored: str, requested: str) -> bool:
        """Whether a stored stream id is in the stream the caller asked for.

        A caller asks by bare name (``"orders"``) or by scoped name; a stored
        id may be bare (a v1 row) or scoped (a v2 row). A bare request is the
        cross-account read and matches either form on the bare name. A scoped
        request names ONE account and matches only itself — falling back to a
        bare-name match here would widen a scoped read into every account,
        which is precisely the cross-account leak the scope exists to close.
        """
        if not requested or requested == "*":
            return True
        if stored == requested:
            return True
        if ":" in requested:
            # Scoped requests are exact. A stored id from any other account,
            # or a bare id, does not satisfy it.
            return False
        # A bare request matches the bare name of a scoped id, so a caller
        # that does not name an account still sees every account's rows in
        # that stream — and the legacy rows, which are bare by construction.
        return stream_suffix(stored) == requested

    def _event_count(self) -> int:
        row = self._conn.execute("SELECT COUNT(*) FROM events").fetchone()
        return int(row[0]) if row else 0

    def _events_since_last_snapshot(self) -> int:
        """Events appended since the newest snapshot of the store's own scope.

        A reopen must not treat the whole log as "since the last snapshot" —
        the snapshot already folds everything below its ``through_sequence``,
        and counting it again would fire a periodic snapshot immediately. The
        counter is the distance from the snapshot's position, and zero when a
        snapshot already covers the whole log.
        """
        snapshot = self.latest_snapshot()
        if snapshot is None:
            return self._event_count()
        return max(0, self._event_count() - snapshot.through_sequence)

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
        lifecycle event types, and ``"system"`` for anything else. The stored
        value is scoped to this store's account:
        ``f"{broker_id}:{account_id}:{stream}"``. Callers that route events
        to custom streams (e.g. ``"risk"``) may supply the override explicitly;
        the scope is still applied, so an override names a *stream*, not a
        partition.

        The row is written with ``prev_hash`` bound to the current chain head,
        so the log is tamper-evident the moment it lands rather than only
        after a later audit.

        The ``event_id`` is a fresh UUID4 minted on each append call —
        each logical append is a distinct row even if the same Python
        object is appended twice (the caller controls deduplication at
        a higher level via idempotency guards).
        """
        bare = stream_id if stream_id is not None else _infer_stream(event)
        sid = self._scope(bare)
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
            self._events_since_snapshot += 1
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

        The hash function is chosen by the row's own ``schema_version``, so a
        v1 row (bare stream id) and a v2 row (account-scoped stream id) are each
        verified as they were written. A row of a version this build carries no
        hash function for is yielded UNVERIFIED: no decoder exists for that
        version either, so :func:`_decode` refuses it with a message about the
        missing decoder — a stronger and more honest verdict than a fabricated
        chain error about a digest that was never computed.

        This runs on the READ path. Recording a chain that nothing ever checks
        would leave the store exactly as tamperable as it was before, so an
        inconsistency raises :class:`EventChainError` here rather than being
        logged and passed over.
        """
        cursor = self._conn.execute(
            f"SELECT {_CHAIN_COLUMNS} FROM events ORDER BY sequence",
        )

        want_all = (not stream) or stream == "*"
        hashable = _hash_versions()
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
            if int(schema_version) in hashable:
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
            if want_all or self._matches_stream(stream_id, stream):
                yield sequence, payload, schema_version

    def replay(
        self, stream: str = "orders", after_sequence: int | None = None,
    ) -> Iterator[DomainEvent]:
        """Yield events in *stream*, in sequence order.

        *stream* may be given bare (``"orders"``) or account-scoped
        (``"PAPER:acc1:orders"``); a bare name matches every account's rows in
        that stream, and a scoped name only its own. An empty string or
        ``"*"`` returns every stream.

        *after_sequence* yields only rows strictly above it, which is what makes
        a snapshot fold cheap: recovery passes the snapshot's
        ``through_sequence`` and the store reads the tail instead of the whole
        log, so restart cost stops growing with uptime. It is a keyword with a
        ``None`` default, so every existing call site is unchanged.

        The hash chain is verified as the log is read, so a row that has been
        edited, reordered, or removed from the middle stops the replay with
        :class:`EventChainError`. A rebuild from a log that cannot be proven
        faithful is not a rebuild worth trusting.

        **The chain is still verified from the head.** Seeking must not become
        a hole in the integrity check: :meth:`_verify_chain` walks the whole
        stream, so a row edited *below* the resume point still fails the replay.
        Only the decode of already-verified rows is skipped.

        Raises :class:`EventDecodeError` on the first row this build cannot
        read. A corrupt or unrecognised row is NOT skipped: the caller is
        rebuilding the authoritative book, and a silently dropped fill is a
        wrong balance rather than a missing one.
        """
        for sequence, payload, schema_version in self._verify_chain(stream):
            if after_sequence is not None and sequence <= after_sequence:
                continue
            yield _decode(payload, schema_version)

    # ------------------------------------------------------------------
    # Snapshots
    # ------------------------------------------------------------------

    def write_snapshot(
        self,
        state: object | None = None,
        *,
        stream: str = "orders",
        reason: str = "periodic",
        through_sequence: int | None = None,
    ) -> Snapshot | None:
        """Fold *state* into a snapshot of *stream* and persist it.

        A snapshot is written when the session reaches READY and every
        :data:`SNAPSHOT_INTERVAL` events appended since the last one — the
        periodic path is :meth:`maybe_snapshot`, which this method performs
        unconditionally when called by name.

        ``through_sequence`` defaults to the store's last appended sequence,
        and replay starts strictly after it. The snapshot's payload is
        canonical JSON, never pickle, so a later build can read it without
        reconstructing the object graph that wrote it. ``state=None`` writes
        the READY snapshot, which records the position and no folded state.

        Returns the persisted :class:`Snapshot`, or ``None`` when there is
        nothing to snapshot — a store that has never appended an event has no
        position to fold.
        """
        sid = self._scope(stream)
        # The position is taken on the SID, which is the stream the snapshot
        # will actually be keyed under — not the bare name the caller passed.
        # A scoped store snapshotting an un-scoped log must still find the
        # position of the rows it is folding.
        if through_sequence is None:
            through_sequence = self.last_sequence(sid) or self.last_sequence(stream)
        if through_sequence is None or through_sequence <= 0:
            return None

        snapshot = (
            Snapshot.empty(sid, through_sequence, reason=reason)
            if state is None
            else Snapshot(
                stream_id=sid,
                through_sequence=int(through_sequence),
                payload=state if isinstance(state, dict) else {"state": state},
                created_at=datetime.now(UTC).isoformat(),
                reason=reason,
            )
        )
        with self._lock:
            self._conn.execute(
                "INSERT OR REPLACE INTO snapshots "
                "(stream_id, through_sequence, payload, created_at, reason) "
                "VALUES (?, ?, ?, ?, ?)",
                (
                    snapshot.stream_id,
                    snapshot.through_sequence,
                    snapshot.payload_json(),
                    snapshot.created_at,
                    snapshot.reason,
                ),
            )
            self._conn.commit()
        self._events_since_snapshot = 0
        return snapshot

    def maybe_snapshot(
        self,
        state: object | None = None,
        *,
        stream: str = "orders",
    ) -> Snapshot | None:
        """Write a periodic snapshot once :data:`SNAPSHOT_INTERVAL` events pass.

        This is the every-10,000-events half of the spec; the READY half is a
        direct :meth:`write_snapshot` call with ``reason="ready"``. Returns the
        snapshot written this call, or ``None`` when the interval has not
        elapsed — so a caller can tell "a snapshot landed" from "it did not".
        """
        if self._events_since_snapshot < SNAPSHOT_INTERVAL:
            return None
        return self.write_snapshot(state, stream=stream, reason="periodic")

    def latest_snapshot(self, stream: str = "orders") -> Snapshot | None:
        """The newest snapshot for *stream*, or ``None`` when there is none.

        Recovery reads this and replays strictly after
        ``through_sequence`` — the log below a snapshot is never read except
        by the snapshot itself.
        """
        sid = self._scope(stream)
        rows = self._conn.execute(
            "SELECT stream_id, through_sequence, payload, created_at, reason "
            "FROM snapshots WHERE stream_id = ? "
            "ORDER BY through_sequence DESC LIMIT 1",
            (sid,),
        ).fetchall()
        if not rows:
            # An un-scoped caller reading a scoped store's snapshots, or vice
            # versa: fall back to the bare name so a READY snapshot written by
            # either scope is found by either.
            rows = self._conn.execute(
                "SELECT stream_id, through_sequence, payload, created_at, reason "
                "FROM snapshots WHERE stream_id LIKE ? "
                "ORDER BY through_sequence DESC LIMIT 1",
                (f"%:{stream_suffix(stream)}",),
            ).fetchall()
        if not rows:
            return None
        stream_id, through_sequence, payload, created_at, reason = rows[0]
        return Snapshot.from_row(
            stream_id, through_sequence, payload, created_at, reason,
        )

    def snapshot_count(self, stream: str = "*") -> int:
        """Number of snapshots held for *stream* (or all streams)."""
        if not stream or stream == "*":
            row = self._conn.execute("SELECT COUNT(*) FROM snapshots").fetchone()
        else:
            row = self._conn.execute(
                "SELECT COUNT(*) FROM snapshots WHERE stream_id = ?",
                (self._scope(stream),),
            ).fetchone()
        return int(row[0]) if row else 0

    # ------------------------------------------------------------------
    # Account-scoped fill dedup
    # ------------------------------------------------------------------

    def _ensure_dedup_table(self, account_id: str | None) -> str:
        """Create the account's dedup partition on first use and cache it.

        The table name comes from :func:`dedup_table_name`, which validates the
        account, so the identifier is never interpolated from caller input
        unchecked. The name is QUOTED in every statement, because an account id
        may legitimately contain a character that is not legal in a bare
        SQLite identifier (``acc-1``) — quoting is what makes the same id
        usable in a stream name and in a table name.

        ``IF NOT EXISTS`` makes this idempotent, so a store that reopens a
        database another store already partitioned does not need to have been
        told about it: the table is where the other store left it, and this
        store reads it.
        """
        name = dedup_table_name(account_id)
        if name in self._dedup_tables:
            return name
        with self._lock:
            self._conn.execute(
                f'CREATE TABLE IF NOT EXISTS "{name}" ({_DEDUP_COLUMNS})',
            )
            self._conn.execute(
                f'CREATE UNIQUE INDEX IF NOT EXISTS "{name}_key" '
                f'ON "{name}" (fingerprint)',
            )
            self._conn.commit()
        self._dedup_tables.add(name)
        return name

    def fill_seen(self, fill: object, *, account_id: str | None = None) -> bool:
        """True when *fill*'s fingerprint is already recorded for the account.

        Partitioned by account: the same venue trade id under two accounts is
        two executions, and the same account with a re-published fill is one.
        ``account_id`` defaults to the store's own scope, so a caller that
        already scoped the store does not have to pass it again.
        """
        account = account_id if account_id is not None else self.account_id
        table = self._ensure_dedup_table(account)
        key = dedup_key(account, fill_fingerprint(fill))
        with self._lock:
            row = self._conn.execute(
                f'SELECT 1 FROM "{table}" WHERE fingerprint = ?',
                (json.dumps(key[1], sort_keys=True),),
            ).fetchone()
        return row is not None

    def record_fill(self, fill: object, *, account_id: str | None = None) -> bool:
        """Record *fill*'s fingerprint for the account; False if already seen.

        Recording AFTER the durable write is deliberate: recording first would
        make the venue's retry of a write that failed look like a duplicate
        forever, and the fill would be lost rather than double-applied.
        """
        account = account_id if account_id is not None else self.account_id
        table = self._ensure_dedup_table(account)
        key = dedup_key(account, fill_fingerprint(fill))
        encoded = json.dumps(key[1], sort_keys=True)
        with self._lock:
            try:
                self._conn.execute(
                    f'INSERT INTO "{table}" (fingerprint, seen_at) VALUES (?, ?)',
                    (encoded, datetime.now(UTC).isoformat()),
                )
                self._conn.commit()
                return True
            except sqlite3.IntegrityError:
                # The composite (account, fingerprint) key already exists —
                # this is a re-delivery, not a new execution.
                return False

    def fill_dedup_count(self, account_id: str | None = None) -> int:
        """Number of fingerprints held in the account's dedup partition.

        A partition that this store has never touched may still hold rows
        written by an earlier store on the same database, so the table is
        created (idempotently) and read rather than assumed empty.
        """
        account = account_id if account_id is not None else self.account_id
        table = self._ensure_dedup_table(account)
        row = self._conn.execute(f'SELECT COUNT(*) FROM "{table}"').fetchone()
        return int(row[0]) if row else 0

    # ------------------------------------------------------------------
    # Diagnostics
    # ------------------------------------------------------------------

    def count(self, stream: str = "orders") -> int:
        """Return the number of events in *stream* (or total if ``"*"``).

        A bare *stream* name counts every account's rows in that stream; a
        scoped name counts only its own. The scoped lookup does NOT fall back
        to a suffix match: a scoped caller asked for one account, and answering
        it with every account's count is the exact defect the scope exists to
        remove.
        """
        if stream and stream != "*":
            row = self._conn.execute(
                "SELECT COUNT(*) FROM events WHERE stream_id = ?",
                (stream,),
            ).fetchone()
            if row and row[0]:
                return int(row[0])
            if ":" in stream:
                # A scoped name is exact or nothing. It is not widened into a
                # cross-account read.
                return 0
            # A bare name is the cross-account read, so it matches the suffix
            # of a scoped id rather than only the bare ids on disk.
            row = self._conn.execute(
                "SELECT COUNT(*) FROM events WHERE stream_id LIKE ?",
                (f"%:{stream}",),
            ).fetchone()
        else:
            row = self._conn.execute("SELECT COUNT(*) FROM events").fetchone()
        return int(row[0]) if row else 0

    def last_sequence(self, stream: str = "orders") -> int:
        """Return the highest sequence number in *stream*, or 0 if empty."""
        if stream and stream != "*":
            row = self._conn.execute(
                "SELECT MAX(sequence) FROM events WHERE stream_id = ?",
                (stream,),
            ).fetchone()
            if row and row[0] is not None:
                return int(row[0])
            if ":" in stream:
                return 0
            row = self._conn.execute(
                "SELECT MAX(sequence) FROM events WHERE stream_id LIKE ?",
                (f"%:{stream}",),
            ).fetchone()
        else:
            row = self._conn.execute("SELECT MAX(sequence) FROM events").fetchone()
        return int(row[0]) if (row and row[0] is not None) else 0

    def close(self) -> None:
        """Close the underlying SQLite connection."""
        self._conn.close()


#: Columns fetched for verification, in a fixed order.
_CHAIN_COLUMNS = (
    "sequence, stream_id, event_type, event_id, correlation_id, "
    "payload, created_at, schema_version, prev_hash, event_hash"
)
