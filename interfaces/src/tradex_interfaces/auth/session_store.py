"""Server-side session store — in-memory, single-process.

Holds server-side session records keyed by SHA-256 hash of the raw session ID.
The raw ID is returned once (to be placed in an HttpOnly cookie) and is never
stored here — the store holds only the verifier hash.

ponytail: process-local; ceiling is a single Uvicorn worker. Upgrade path:
replace InMemorySessionStore with a shared-backend implementation (Redis/DB)
behind the same interface. The session/revocation contract is interface-stable.
"""
from __future__ import annotations

import hashlib
import secrets
import threading
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta

SESSION_ABSOLUTE_TTL = timedelta(hours=12)
SESSION_IDLE_TTL = timedelta(hours=2)
_SESSION_ID_BYTES = 32  # 256 bits of entropy


@dataclass
class SessionRecord:
    """Server-side session record. Never includes the raw session ID."""

    session_id_hash: str    # SHA-256 hex of the raw ID; the raw ID is NEVER stored
    subject: str            # authenticated principal name
    csrf_secret: str        # 32-byte random hex; used only for CSRF token derivation
    created_at: datetime
    last_seen: datetime
    expires_at: datetime
    idle_expires_at: datetime
    auth_version: int = 0
    revoked: bool = False
    revoked_at: datetime | None = None


class InMemorySessionStore:
    """Thread-safe in-memory session store.

    Stores session records keyed by SHA-256 of the raw session identifier.
    Caller must treat ``create``'s returned ``raw_id`` as a bearer credential
    — place it in a cookie and do not log it.

    ponytail: not shared across workers; no persistence across restarts.
    """

    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._records: dict[str, SessionRecord] = {}

    def create(self, subject: str) -> tuple[str, SessionRecord]:
        """Create a new session. Returns ``(raw_session_id, record)``.

        The raw session ID must be placed in an HttpOnly cookie and must
        not be logged or stored anywhere else.
        """
        raw_id = secrets.token_hex(_SESSION_ID_BYTES)
        id_hash = hashlib.sha256(raw_id.encode()).hexdigest()
        csrf_secret = secrets.token_hex(32)
        now = datetime.now(UTC)
        record = SessionRecord(
            session_id_hash=id_hash,
            subject=subject,
            csrf_secret=csrf_secret,
            created_at=now,
            last_seen=now,
            expires_at=now + SESSION_ABSOLUTE_TTL,
            idle_expires_at=now + SESSION_IDLE_TTL,
        )
        with self._lock:
            self._records[id_hash] = record
        return raw_id, record

    def lookup(self, raw_id: str) -> SessionRecord | None:
        """Look up a session by its raw ID.

        Returns ``None`` when absent, revoked, or past either expiry deadline.
        Refreshes idle expiry on every successful lookup.
        """
        id_hash = hashlib.sha256(raw_id.encode()).hexdigest()
        now = datetime.now(UTC)
        with self._lock:
            record = self._records.get(id_hash)
            if record is None:
                return None
            if record.revoked:
                return None
            if now >= record.expires_at:
                return None
            if now >= record.idle_expires_at:
                return None
            # Refresh idle window on valid access
            record.last_seen = now
            record.idle_expires_at = now + SESSION_IDLE_TTL
            return record

    def revoke(self, raw_id: str) -> bool:
        """Revoke a session by its raw ID. Returns ``True`` if found."""
        id_hash = hashlib.sha256(raw_id.encode()).hexdigest()
        now = datetime.now(UTC)
        with self._lock:
            record = self._records.get(id_hash)
            if record is None:
                return False
            record.revoked = True
            record.revoked_at = now
            return True

    def active_count(self) -> int:
        """Return count of sessions that are not revoked."""
        with self._lock:
            return sum(1 for r in self._records.values() if not r.revoked)


__all__ = [
    "InMemorySessionStore",
    "SessionRecord",
    "SESSION_ABSOLUTE_TTL",
    "SESSION_IDLE_TTL",
]
