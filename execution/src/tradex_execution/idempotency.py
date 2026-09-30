"""Idempotency guard — extracted from engine.py (PE-6).

Provides the IdempotencyGuard protocol and implementations for
in-process correlation-id dedupe with reservation + release.
Also provides FillDedup — bounded LRU dedup for applied fill events — and
DurableFillDedup, which backs the same interface with the event store's
durable fill index so dedup survives a process restart.
"""

from __future__ import annotations

import threading
from collections import OrderedDict
from collections.abc import Callable
from dataclasses import dataclass
from decimal import Decimal
from typing import Any, Protocol, runtime_checkable

from tradex_domain.value_objects import CorrelationId


@dataclass(frozen=True, slots=True)
class IdempotencyDuplicate:
    """A completed idempotent request — its recorded result is returned."""

    result: Any


class IdempotencyKeyReuseMismatch(RuntimeError):
    """A completed/reserved idempotency key reused with a different request.

    Raised by guards when ``check_and_reserve`` is called with a
    ``request_hash`` that differs from the hash bound to the key at its
    original reservation. The caller maps this to a 409 Conflict — never
    to a silent replay or a second mutation (N2, principal review).
    """


class IdempotencyInflight(RuntimeError):
    """A key is reserved but not yet completed — the original request is
    still processing (or a crash left residue).

    A subclass of ``RuntimeError`` so pre-existing "already reserved"
    assertions keep passing; callers that want the deterministic response
    catch this type. The reservation must NOT be released — the original
    request owns it (N5, principal review).
    """


@runtime_checkable
class IdempotencyGuard(Protocol):
    def check_and_reserve(
        self,
        correlation_id: CorrelationId,
        request_hash: str | None = None,
    ) -> IdempotencyDuplicate | None: ...
    def record_result(
        self, correlation_id: CorrelationId, result: Any,
    ) -> None: ...
    def release(self, correlation_id: CorrelationId) -> None: ...


class MemoryIdempotencyGuard:
    """In-process correlation-id dedupe with reservation + release."""

    def __init__(self) -> None:
        self._reserved: set[str] = set()
        self._completed: dict[str, Any] = {}
        #: Request-hash binding (N2): the hash recorded at reservation time
        #: (or at completion when the reservation never bound one).
        self._pending_hash: dict[str, str] = {}
        self._completed_hash: dict[str, str] = {}
        self._lock = threading.RLock()

    def _hash_mismatch(
        self, stored: str | None, incoming: str | None, key: str,
    ) -> None:
        if incoming is not None and stored is not None and stored != incoming:
            raise IdempotencyKeyReuseMismatch(
                f"idempotency key {key} was already used with a different "
                "request (request-hash mismatch)"
            )

    def check_and_reserve(
        self,
        correlation_id: CorrelationId,
        request_hash: str | None = None,
    ) -> IdempotencyDuplicate | None:
        key = str(correlation_id.value)
        with self._lock:
            if key in self._completed:
                self._hash_mismatch(
                    self._completed_hash.get(key), request_hash, key,
                )
                return IdempotencyDuplicate(result=self._completed[key])
            if key in self._reserved:
                self._hash_mismatch(
                    self._pending_hash.get(key), request_hash, key,
                )
                raise IdempotencyInflight(
                    f"idempotency key is already reserved: {key}",
                )
            self._reserved.add(key)
            if request_hash is not None:
                self._pending_hash[key] = request_hash
            return None

    def record_result(
        self, correlation_id: CorrelationId, result: Any,
    ) -> None:
        key = str(correlation_id.value)
        with self._lock:
            self._completed[key] = result
            if key in self._pending_hash:
                self._completed_hash[key] = self._pending_hash.pop(key)
            self._reserved.discard(key)

    def release(self, correlation_id: CorrelationId) -> None:
        with self._lock:
            key = str(correlation_id.value)
            self._reserved.discard(key)
            self._pending_hash.pop(key, None)


class FillDedup:
    """Bounded LRU dedup for applied fill events.

    Each fill is fingerprinted by ``(fill_id,)`` when the venue provides
    a trade id, or ``(order_id, side, qty, price)`` as fallback.  A
    re-published fill with the same fingerprint is a duplicate and must
    not be applied twice.

    The LRU is bounded at *max_size* (default 50 000).  On overflow the
    oldest fingerprint is evicted and *on_evict* (if provided) is called
    with no arguments — typically used to bump a metrics counter.

    Thread-safe: all mutations are serialized under a single lock.
    """

    def __init__(
        self,
        max_size: int = 50_000,
        on_evict: Callable[[], None] | None = None,
    ) -> None:
        self._lru: OrderedDict[tuple, None] = OrderedDict()
        self._max_size = max_size
        self._on_evict = on_evict
        self._lock = threading.Lock()

    @staticmethod
    def fingerprint(fill: Any) -> tuple:
        """Stable dedup key for a fill."""
        if getattr(fill, "fill_id", None) is not None:
            return (fill.fill_id,)
        return (
            fill.order_id.value,
            fill.side.value,
            str(fill.quantity.value),
            str(fill.price.value),
        )

    def seen(self, fill: Any) -> bool:
        """True if this fill was already applied, without recording it.

        Lets a caller decide when to commit the fingerprint: recording before
        the durable write means a failed write makes the venue's retry look like
        a duplicate forever, losing the fill permanently.
        """
        key = self.fingerprint(fill)
        with self._lock:
            if key in self._lru:
                self._lru.move_to_end(key)
                return True
        return False

    def check_and_record(self, fill: Any) -> bool:
        """Return True if *fill* is a duplicate (skip apply), False if fresh."""
        key = self.fingerprint(fill)
        evicted = False
        with self._lock:
            if key in self._lru:
                self._lru.move_to_end(key)
                return True
            self._lru[key] = None
            if len(self._lru) > self._max_size:
                self._lru.popitem(last=False)
                evicted = True
        if evicted and self._on_evict is not None:
            self._on_evict()
        return False


@runtime_checkable
class FillDedupStore(Protocol):
    """The durable fill-dedup seam :class:`DurableFillDedup` reads through.

    The two methods are the ones
    :class:`~tradex_execution.sqlite_event_store.SQLiteEventStore` already
    carries (``fill_seen`` / ``record_fill``). They are declared as a Protocol
    rather than imported so this module keeps no import edge to the store, and
    so a store is not required to be a ``SQLiteEventStore`` to back dedup.
    """

    def fill_seen(self, fill: object, *, account_id: str | None = None) -> bool: ...
    def record_fill(
        self, fill: object, *, account_id: str | None = None,
    ) -> bool: ...


class DurableFillDedup:
    """Fill dedup that survives the process, falling back to :class:`FillDedup`.

    :class:`FillDedup` is a bounded LRU held in memory, so every fingerprint
    in it dies with the process. A broker that re-publishes a fill after a
    crash-and-restart finds an empty LRU, the fill reads as fresh, and it is
    applied a second time to a book that already holds it. That is a real-money
    defect, and it is the reason the store's ``fill_seen`` / ``record_fill``
    existed with no production caller.

    With a store, every question is asked of the store, so the answer
    survives a restart. Without one — backtest and paper, where there is no
    store and nothing to survive — the LRU answers exactly as it always has.

    **The two paths fingerprint identically.** This class does NOT compute its
    own key: it hands the fill object to the store, which uses
    :func:`~tradex_execution.sqlite_event_store.fill_fingerprint`. That
    function is the same scheme :meth:`FillDedup.fingerprint` implements
    (``(fill_id,)`` when the venue provides one, else the
    ``(order_id, side, qty, price)`` tuple), so a fill the LRU would call
    fresh is one the store calls fresh too. The static check below pins that
    agreement so a change to one scheme cannot silently diverge from the other.

    The LRU is kept even in durable mode, as a write-through cache. It is what
    keeps the steady-state cost off SQLite — a live engine applies thousands
    of fills an hour, and answering every one of them with a SELECT and an
    INSERT would be a regression in the hot path. It is consulted first and
    the store second, so a restart (empty LRU, full store) costs one store
    round trip per fill until the LRU refills, and nothing is wrong at any
    point in between.

    ``check_and_record`` and ``seen`` are the whole interface, identical to
    :class:`FillDedup`, so a caller can be pointed at this class without
    changing how it calls anything.
    """

    def __init__(
        self,
        store: FillDedupStore | None = None,
        max_size: int = 50_000,
        on_evict: Callable[[], None] | None = None,
        account_id: str | None = None,
    ) -> None:
        self._store = store if isinstance(store, FillDedupStore) else None
        self._lru = FillDedup(max_size=max_size, on_evict=on_evict)
        self._account_id = account_id
        self._lock = threading.Lock()

    @property
    def is_durable(self) -> bool:
        """True when a store is backing the dedup (and it survives a restart)."""
        return self._store is not None

    @property
    def store(self) -> FillDedupStore | None:
        return self._store

    @staticmethod
    def fingerprint(fill: Any) -> tuple:
        """The one dedup key, for a caller that wants it without a store.

        Delegates to :meth:`FillDedup.fingerprint` rather than restating the
        scheme: the durable path has the store compute its own, and this is the
        in-process half of the same rule. A store that ever disagreed with this
        is the bug :meth:`fingerprints_agree` exists to catch.
        """
        return FillDedup.fingerprint(fill)

    def seen(self, fill: Any) -> bool:
        """True if this fill was already applied, without recording it.

        Same contract as :meth:`FillDedup.seen`, and the same reason it
        exists: a caller commits the fingerprint only after its durable write
        has landed, so a failed write must not make the venue's retry look
        like a duplicate forever.
        """
        with self._lock:
            if self._lru.seen(fill):
                return True
            if self._store is None:
                return False
            # The LRU is a cache of the store, not a second opinion: when the
            # LRU misses, the store decides. Recording nothing here is what
            # lets the caller retry.
            return bool(
                self._store.fill_seen(fill, account_id=self._account_id),
            )

    def check_and_record(self, fill: Any) -> bool:
        """Return True if *fill* is a duplicate (skip apply), False if fresh."""
        with self._lock:
            if self._lru.check_and_record(fill):
                return True
            if self._store is None:
                return False
            # The store's own insert is the authority, and it is atomic: a
            # concurrent writer that got there first makes this return False,
            # which is the duplicate verdict, so two threads racing on one
            # fill cannot both book it.
            return not bool(self._store.record_fill(
                fill, account_id=self._account_id,
            ))


def fingerprints_agree() -> bool:
    """Whether the in-process and durable fingerprint schemes are the same.

    :meth:`FillDedup.fingerprint` and
    :func:`~tradex_execution.sqlite_event_store.fill_fingerprint` are two
    implementations of one rule, in two files, and nothing in the type system
    connects them. This is the runtime check that they have not drifted: a
    change to either that altered the key would make a fill the LRU considers
    fresh read as a duplicate in the store, or the reverse, and either way the
    same fill would be applied differently depending on whether a store
    happened to be configured.
    """
    from tradex_execution.sqlite_event_store import fill_fingerprint

    class _Sample:
        """The shape the scheme reads: a fill with or without a venue id."""

        def __init__(self, fill_id, order_id, side, qty, price):
            self.fill_id = fill_id
            self.order_id = type("_Id", (), {"value": order_id})()
            self.side = type("_Enum", (), {"value": side})()
            self.quantity = type("_Value", (), {"value": Decimal(qty)})()
            self.price = type("_Value", (), {"value": Decimal(price)})()

    samples = (
        _Sample("trade-1", "o-1", "BUY", "10", "2500"),
        _Sample(None, "o-1", "BUY", "10", "2500"),
        _Sample(None, "o-2", "SELL", "10.5", "2499.75"),
    )
    return all(
        FillDedup.fingerprint(s) == fill_fingerprint(s) for s in samples
    )

