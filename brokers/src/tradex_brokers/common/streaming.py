"""ReconnectingStreamBackend — re-subscribes active subscriptions after drop."""

from __future__ import annotations

from contextlib import suppress
from typing import Any


class ReconnectingStreamBackend:
    """Wraps any stream backend; ``reconnect()`` restores active subs."""

    def __init__(self, backend: Any) -> None:
        self._backend = backend
        self._active: list[tuple[str, tuple[Any, ...], Any]] = []

    def subscribe_quotes(self, instruments: Any, handler: object) -> Any:
        sub = self._backend.subscribe_quotes(instruments, handler)
        self._active.append(("quotes", (instruments, handler), sub))
        return sub

    def subscribe_orders(self, handler: object) -> Any:
        sub = self._backend.subscribe_orders(handler)
        self._active.append(("orders", (handler,), sub))
        return sub

    def subscribe_positions(self, handler: object) -> Any:
        sub = self._backend.subscribe_positions(handler)
        self._active.append(("positions", (handler,), sub))
        return sub

    def unsubscribe(self, subscription: Any) -> None:
        self._backend.unsubscribe(subscription)
        self._active = [
            rec for rec in self._active if rec[2] is not subscription
        ]

    def reconnect(self) -> None:
        """Drop every subscription and re-subscribe (same kinds/handlers)."""
        for _kind, _args, sub in list(self._active):
            with suppress(Exception):  # noqa: BLE001
                self._backend.unsubscribe(sub)
        restored: list[tuple[str, tuple[Any, ...], Any]] = []
        for kind, args, _sub in self._active:
            fn = getattr(self._backend, f"subscribe_{kind}")
            new_sub = fn(*args)
            restored.append((kind, args, new_sub))
        self._active = restored

    def close(self) -> None:
        self._backend.close()


__all__ = ["ReconnectingStreamBackend"]
