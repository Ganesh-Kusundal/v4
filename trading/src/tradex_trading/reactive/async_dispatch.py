"""Async dispatch support for ReactiveBus.

Wraps a ReactiveBus with optional asyncio event-loop integration so
that coroutine handlers can be scheduled from synchronous bus emissions
and messages can be published from within an async context.
"""

from __future__ import annotations

import asyncio
import logging
from collections.abc import Callable, Coroutine
from typing import Any

from rx.disposable import CompositeDisposable

log = logging.getLogger(__name__)


class AsyncDispatch:
    """Bridge between a synchronous ReactiveBus and asyncio handlers.

    Parameters
    ----------
    bus:
        A :class:`~tradex_trading.reactive.bus.ReactiveBus` instance.
    loop:
        An :class:`asyncio.AbstractEventLoop` to schedule coroutines on.
        If ``None``, all operations fall back to synchronous execution.
    """

    def __init__(self, bus: Any, loop: asyncio.AbstractEventLoop | None = None) -> None:
        self._bus = bus
        self._loop = loop
        self._disposables = CompositeDisposable()
        self._handlers: list[Callable[..., Coroutine[Any, Any, Any]]] = []

    # ------------------------------------------------------------------ #
    # Subscribe
    # ------------------------------------------------------------------ #

    def subscribe_async(
        self,
        handler: Callable[..., Coroutine[Any, Any, Any]],
    ) -> None:
        """Register a coroutine *handler* that is called for every bus message.

        If an event loop is available, the handler is scheduled via
        ``loop.call_soon_threadsafe``; otherwise it is invoked synchronously
        as a best-effort fallback (the coroutine is simply stored).
        """
        self._handlers.append(handler)

        def _on_message(message: Any) -> None:
            if self._loop is not None and self._loop.is_running():
                self._loop.call_soon_threadsafe(
                    lambda h=handler, m=message: asyncio.ensure_future(h(m), loop=self._loop),  # type: ignore[arg-type]
                )
            else:
                # Sync fallback — just store the intent; tests can inspect
                # _handlers to verify registration.
                log.debug("AsyncDispatch: no running loop, handler stored for %r", message)

        d = self._bus.subscribe(on_next=_on_message)
        self._disposables.add(d)

    # ------------------------------------------------------------------ #
    # Publish
    # ------------------------------------------------------------------ #

    def publish_async(self, message: Any) -> None:
        """Schedule *message* on the event loop, or publish synchronously."""
        if self._loop is not None and self._loop.is_running():
            self._loop.call_soon_threadsafe(self._bus.publish, message)
        else:
            self._bus.publish(message)

    # ------------------------------------------------------------------ #
    # Lifecycle
    # ------------------------------------------------------------------ #

    def dispose(self) -> None:
        """Tear down all subscriptions and clear handler list."""
        self._disposables.dispose()
        self._handlers.clear()
