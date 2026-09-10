"""Idempotency guard — extracted from engine.py (PE-6).

Provides the IdempotencyGuard protocol and implementations for
in-process correlation-id dedupe with reservation + release.
"""

from __future__ import annotations

import threading
from dataclasses import dataclass
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
