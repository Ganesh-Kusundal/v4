"""The SQLite-backed idempotency guard.

The event log (``sqlite_event_store``) is the single persistence authority;
OMS recovery replays it alone.
"""

from __future__ import annotations

import base64
import json
import pickle
import sqlite3
import threading
from pathlib import Path

from tradex_domain.enums import OrderStatus
from tradex_domain.execution import Order, OrderReceipt
from tradex_domain.value_objects import CorrelationId, OrderId

from tradex_execution._sqlite import configure_sqlite
from tradex_execution.sqlite_event_store import (
    ALLOWED_PICKLE_CLASSES,
    _restricted_unpickle,
)

#: Marker prefix for pickle-encoded idempotency results (Order/OrderReceipt objects).
_PICKLE_PREFIX = "__tradex_pickle__:"


# ---------------------------------------------------------------------------
# Restricted unpickling of idempotency results (C3)
# ---------------------------------------------------------------------------
# ``record_result`` pickles an ``Order`` or ``OrderReceipt`` so a restart replays the exact
# post-mutation record. That made the ``result`` column a second pickle sink:
# ``pickle.loads`` there resolved whatever the column named, so anyone able to
# write a row got code execution the next time a correlation id was replayed.
#
# The allowlist is deliberately NARROWER than the event store's. This column
# only ever holds the types :meth:`SQLiteIdempotencyGuard.record_result`
# chooses to pickle — an ``Order`` or ``OrderReceipt`` — plus the value objects, enums and
# instrument dataclasses that ride inside one. It reuses the event store's
# restricted unpickler rather than a second implementation, so there is one
# allowlist to audit and one place to widen deliberately.
#: Classes an idempotency result may legitimately name: an Order's or OrderReceipt's closure.
#: The stdlib leaves (``datetime``/``date``/``Decimal``) and the domain
#: instruments/enums/value-objects are shared with the event store and are
#: listed there; the set below is what is added on top of it.
_IDEMPOTENCY_EXTRA_CLASSES: frozenset[tuple[str, str]] = frozenset({
    ("tradex_domain.execution", "Order"),
    ("tradex_domain.execution", "OrderReceipt"),
})


def _unpickle_idempotency_result(raw: bytes) -> object:
    """Decode a stored idempotency result through the allowlist.

    Raises :class:`UnpickleSecurityError` for a class the build does not
    recognise, which the caller turns into a refusal to replay — the same
    outcome as an undecodable result, and for the same reason: replaying an
    attacker-chosen object into a modify/cancel path is exactly the case where
    guessing is expensive.
    """
    return _restricted_unpickle(
        raw,
        ALLOWED_PICKLE_CLASSES | _IDEMPOTENCY_EXTRA_CLASSES,
    )


class SQLiteIdempotencyGuard:
    """Prevents duplicate order submission using a SQLite-backed set.

    Implements the IdempotencyGuard protocol with reservation + release.
    """

    _CREATE_TABLE = """
    CREATE TABLE IF NOT EXISTS idempotency (
        correlation_id TEXT PRIMARY KEY,
        status TEXT NOT NULL DEFAULT 'reserved',
        result TEXT,
        request_hash TEXT,
        created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
    )
    """

    def __init__(self, db_path: str | Path = ":memory:") -> None:
        if db_path != ":memory:":
            # A fresh runtime directory has no parent for this file, and
            # sqlite3.connect creates the database but never its directory.
            Path(db_path).parent.mkdir(parents=True, exist_ok=True)
        self._conn = sqlite3.connect(
            str(db_path) if db_path != ":memory:" else ":memory:",
            check_same_thread=False,
        )
        configure_sqlite(self._conn)
        self._write_lock = threading.Lock()
        self._conn.execute(self._CREATE_TABLE)
        columns = {
            row[1] for row in self._conn.execute("PRAGMA table_info(idempotency)")
        }
        if "result" not in columns:
            self._conn.execute("ALTER TABLE idempotency ADD COLUMN result TEXT")
        if "request_hash" not in columns:
            self._conn.execute(
                "ALTER TABLE idempotency ADD COLUMN request_hash TEXT"
            )
        self._conn.commit()

    def check_and_reserve(
        self,
        correlation_id: CorrelationId,
        request_hash: str | None = None,
    ) -> object | None:
        """Reserve a correlation id atomically.

        Returns ``None`` when *new*, or an ``IdempotencyDuplicate``-compatible
        object when already completed. ``request_hash``, when provided, is
        bound to the key and compared on later lookups: a completed or
        reserved key reused with a materially different hash raises
        ``IdempotencyKeyReuseMismatch`` instead of replaying (N2). The
        INSERT is attempted first and relies on the PRIMARY KEY constraint —
        a concurrent duplicate insert raises ``IntegrityError`` inside the
        same transaction, closing the SELECT-then-INSERT race window.
        """
        from tradex_execution.engine import (
            IdempotencyDuplicate,
            IdempotencyInflight,
            IdempotencyKeyReuseMismatch,
        )

        key = str(correlation_id.value)
        with self._write_lock:
            try:
                with self._conn:  # commit on success, ROLLBACK on IntegrityError
                    if request_hash is not None:
                        self._conn.execute(
                            "INSERT INTO idempotency (correlation_id, status, request_hash) "
                            "VALUES (?, 'reserved', ?)",
                            (key, request_hash),
                        )
                    else:
                        self._conn.execute(
                            "INSERT INTO idempotency (correlation_id, status) VALUES (?, 'reserved')",
                            (key,),
                        )
                return None  # fresh reservation
            except sqlite3.IntegrityError:
                # Key exists — completed means replay; reserved-but-not-
                # completed after a crash between reserve and record must
                # NOT allow a duplicate through: fail loud so the caller can
                # reconcile instead of silently double-submitting.
                row = self._conn.execute(
                    "SELECT status, result, request_hash FROM idempotency "
                    "WHERE correlation_id = ?",
                    (key,),
                ).fetchone()
                if row is not None:
                    stored_hash = row[2] if len(row) > 2 else None
                    if (
                        request_hash is not None
                        and stored_hash is not None
                        and stored_hash != request_hash
                    ):
                        raise IdempotencyKeyReuseMismatch(
                            f"idempotency key {key} was already used with a "
                            "different request (request-hash mismatch)"
                        )
                if row is not None and row[0] == "completed":
                    if row[1] is None:
                        raise RuntimeError(
                            f"idempotency key {key} is completed without a "
                            "durable result; refusing to submit or replay"
                        )
                    if isinstance(row[1], str) and row[1].startswith(_PICKLE_PREFIX):
                        # Full domain-object results (Order/OrderReceipt for submit/modify/cancel)
                        # are pickled with a marker; anything else is legacy
                        # JSON (a scalar, a dict, or an OrderId marker).
                        try:
                            decoded = _unpickle_idempotency_result(
                                base64.b64decode(row[1][len(_PICKLE_PREFIX):])
                            )
                        except Exception as exc:  # noqa: BLE001
                            raise RuntimeError(
                                f"idempotency key {key} holds an unserializable "
                                f"result; refusing to replay"
                            ) from exc
                        return IdempotencyDuplicate(result=decoded)
                    try:
                        decoded = json.loads(row[1])
                    except (TypeError, json.JSONDecodeError):
                        decoded = row[1]
                    if isinstance(decoded, dict) and "__tradex_order_id__" in decoded:
                        decoded = OrderReceipt(
                            order_id=OrderId(value=str(decoded["__tradex_order_id__"])),
                            status=OrderStatus.SUBMITTED,
                            message="idempotency_replay",
                        )
                    return IdempotencyDuplicate(result=decoded)
                raise IdempotencyInflight(
                    f"idempotency key {key} is reserved but incomplete "
                    f"(crash between reserve and record?) — refusing to "
                    f"duplicate-submit; release() or reconcile manually"
                )

    def record_result(self, correlation_id: CorrelationId, result: object) -> None:
        """Mark a reserved correlation id as completed."""
        key = str(correlation_id.value)
        if isinstance(result, (Order, OrderReceipt)):
            # Full fidelity: the domain object is pickled (local trusted store)
            # so a restart replays the exact record the first call returned.
            encoded = _PICKLE_PREFIX + base64.b64encode(
                pickle.dumps(result)
            ).decode("ascii")
        else:
            encoded = json.dumps(result, default=str)
        with self._write_lock, self._conn:
            self._conn.execute(
                "UPDATE idempotency SET status = 'completed', result = ? "
                "WHERE correlation_id = ?",
                (encoded, key),
            )

    def release(self, correlation_id: CorrelationId) -> None:
        """Release a reserved (but not completed) correlation id."""
        key = str(correlation_id.value)
        with self._write_lock, self._conn:
            self._conn.execute(
                "DELETE FROM idempotency WHERE correlation_id = ? AND status = 'reserved'",
                (key,),
            )

    def close(self) -> None:
        """Close the SQLite connection."""
        self._conn.close()


__all__ = ["SQLiteIdempotencyGuard"]
