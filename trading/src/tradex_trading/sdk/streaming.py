"""Stream subscription wrapper for RxPY disposables.

Provides a session-scoped handle for managing reactive subscriptions.
"""

from __future__ import annotations

import threading
from typing import Any


class StreamSubscription:
    """Wraps an RxPY Disposable as a session-scoped subscription handle."""

    def __init__(self, disposable: Any, topic: str = ""):
        self._disposable = disposable
        self._topic = topic

    def cancel(self) -> None:
        """Dispose the underlying subscription."""
        self._disposable.dispose()

    @property
    def is_active(self) -> bool:
        """Whether the subscription is still active."""
        return not self._disposable.is_disposed

    def __repr__(self) -> str:
        return f"StreamSubscription(topic={self._topic!r}, active={self.is_active})"


class BackendStreamSubscription(StreamSubscription):
    """Session handle over a broker WS backend subscription (string id).

    Broker order/portfolio backends return a string subscription id rather
    than an RxPY disposable. ``cancel()`` routes through the backend's
    ``unsubscribe(id)`` so ``TradingSession.stop()`` (which iterates and
    cancels every handle) never crashes on a bare string.
    """

    def __init__(self, backend: Any, subscription_id: str, topic: str = ""):
        # ``StreamSubscription`` is RxPY-disposable-based; this handle is
        # backend-id-based, so the base ``_disposable`` stays None (defensive
        # guard: nothing on the base may dereference it for this subclass).
        self._disposable = None  # type: ignore[assignment]
        self._backend = backend
        self._subscription_id = subscription_id
        self._topic = topic
        self._cancel_lock = threading.Lock()
        self._cancelled = False

    def cancel(self) -> None:
        # Lock the check-and-set so concurrent cancels (e.g. session.stop()
        # racing a client unsubscribe) never double-invoke the backend.
        with self._cancel_lock:
            if self._cancelled:
                return
            self._cancelled = True
            try:
                self._backend.unsubscribe(self._subscription_id)
            except Exception:  # noqa: BLE001 – best-effort teardown
                pass

    @property
    def is_active(self) -> bool:
        with self._cancel_lock:
            return not self._cancelled

    def __repr__(self) -> str:
        return f"BackendStreamSubscription(topic={self._topic!r}, active={self.is_active})"


__all__ = ["BackendStreamSubscription", "StreamSubscription"]
