"""Test-infra repair: a websocket read that never gets answered must fail.

``starlette.testclient.WebSocketTestSession.receive()`` is
``portal.call(send_rx.receive)`` with no deadline, so a read whose frame never
arrives blocks the test thread until the outer pytest timeout kills the run.
That is not hypothetical. It is how 18 replay tests burned 353s of timeouts
instead of failing in seconds when a monkeypatch target went stale, and the
hang — not the assertion — is what got the same failure twice recorded as
"pre-existing, unrelated".

The websocket suites already express the deadline they want by setting
``ws._receive_timeout``. Starlette 1.6.0 does not define that attribute, so
every assignment was inert. This honours it (default 5.0s, the value Starlette
used to ship and the baseline the suites' 3s/5s/8s/15s values were tuned
against) and raises ``TimeoutError`` in the calling thread, so a missing ack
fails fast and names itself.

It lives here rather than in the repo-root conftest because that file is only
loaded for a root-directory run, and ``cd trading && python -m pytest`` is a
documented workflow that needs the deadline just as much.
"""

from __future__ import annotations

from typing import Any

#: Seconds a websocket read waits for a frame before failing. Per-test
#: ``ws._receive_timeout`` overrides it (a suite that expects to wait longer
#: sets a larger value; a suite that expects an immediate ack can lower it).
DEFAULT_WS_RECEIVE_TIMEOUT = 5.0

try:
    import anyio
    from starlette.testclient import WebSocketTestSession
except ImportError:  # pragma: no cover - starlette is an optional test dep
    WebSocketTestSession = None  # type: ignore[assignment,misc]


def _receive_with_deadline(self: Any, *_args: Any, **_kwargs: Any) -> Any:
    """``WebSocketTestSession.receive`` that cannot block forever.

    The wait runs on the portal's event loop under ``anyio.fail_after``, so a
    timeout cancels the pending receive instead of leaving a task holding the
    stream, and the error surfaces in the calling thread.
    """
    timeout = getattr(self, "_receive_timeout", None) or DEFAULT_WS_RECEIVE_TIMEOUT

    async def _await_frame() -> Any:
        with anyio.fail_after(timeout):
            return await self._send_rx.receive()

    try:
        return self.portal.call(_await_frame)
    except TimeoutError as exc:
        raise TimeoutError(
            f"no websocket frame within {timeout}s: the server never sent one. "
            f"A missing ack is the usual cause — check the handler that was "
            f"supposed to answer. A test that genuinely waits longer should set "
            f"ws._receive_timeout above {timeout}."
        ) from exc


if WebSocketTestSession is not None and not getattr(
    WebSocketTestSession.receive, "_tradex_deadline", False
):
    _receive_with_deadline._tradex_deadline = True  # type: ignore[attr-defined]
    WebSocketTestSession.receive = _receive_with_deadline  # type: ignore[method-assign]
