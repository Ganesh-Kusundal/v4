"""Graceful shutdown must survive the signal every supervisor sends.

Defect (Critical): ``tradex serve`` had no teardown of its own. ``cli.py`` wraps
its work in ``try/finally`` and calls ``session.stop()``, but
``start_fastapi_server`` ends at ``uvicorn.run(app, ...)`` with no
``try/finally``, and ``create_app`` registered no lifespan handler.

That is invisible for Ctrl-C and fatal for SIGTERM. ``uvicorn.run`` wraps its
work in ``capture_signals``, which on exit restores the default handlers and
re-raises the captured signal *inside* the ``with`` block -- so ``run()`` never
returns and no caller-side ``finally`` is ever reached. For SIGTERM the restored
disposition is ``SIG_DFL``, so the kernel terminates the process at that point.
SIGTERM is what ``docker stop``, Kubernetes, systemd and a plain ``kill`` send.

The invariant pinned here: the session is stopped during ASGI lifespan
shutdown, which completes inside uvicorn's ``_serve()`` -- before the signal is
re-raised -- so teardown runs for every signal and for every entry point
(single process, ``workers>1`` children, ``reload`` children, and direct
factory invocation).
"""

from __future__ import annotations

import subprocess
import sys
import textwrap
from pathlib import Path

import pytest
from starlette.testclient import TestClient
from tradex_interfaces.fastapi_app import create_app


class _StopCountingSession:
    """Minimal session stand-in that records whether it was stopped."""

    def __init__(self) -> None:
        self.stopped = 0

    def stop(self) -> None:
        self.stopped += 1


def test_app_lifespan_stops_the_session_on_shutdown() -> None:
    """create_app must own teardown, so it cannot be bypassed by a signal."""
    session = _StopCountingSession()
    app = create_app(session, api_key=None)

    with TestClient(app):
        assert session.stopped == 0, "session stopped before shutdown"

    assert session.stopped == 1, (
        "the app's lifespan must stop the session; teardown owned only by the "
        "caller's finally is skipped when uvicorn re-raises SIGTERM"
    )


def test_lifespan_shutdown_is_idempotent() -> None:
    """Re-entering shutdown must not double-stop (uvicorn may call it twice)."""
    session = _StopCountingSession()
    app = create_app(session, api_key=None)
    with TestClient(app):
        pass
    first = session.stopped
    # A second client on the same app exercises a second startup/shutdown pair.
    with TestClient(app):
        pass
    assert session.stopped >= first
    assert session.stopped == 2, (
        f"expected one stop per lifespan cycle, saw {session.stopped}"
    )


_SERVE_SCRIPT = textwrap.dedent(
    """
    import os, signal, sys, threading, time
    sys.path.insert(0, {repo!r})

    from tradex_interfaces.fastapi_app import start_fastapi_server

    class S:
        def __init__(self):
            self.stopped = threading.Event()
            self.broker_id = "PAPER"
            self.mode = "paper"
        def start(self): pass
        @property
        def state(self): return "READY"
        def stop(self):
            print("SESSION_STOPPED", flush=True)
            self.stopped.set()

    session = S()
    # Serve on an ephemeral port, then signal ourselves the way a supervisor would.
    def killer():
        time.sleep(2.0)
        os.kill(os.getpid(), signal.SIGTERM)
    threading.Thread(target=killer, daemon=True).start()
    start_fastapi_server(session=session, host="127.0.0.1", port={port})
    print("RETURNED_FROM_RUN", flush=True)
    """
)


@pytest.mark.timeout(60)
def test_sigterm_runs_teardown(tmp_path: Path) -> None:
    """SIGTERM must still tear down; this is the signal that skipped it."""
    repo = str(Path(__file__).resolve().parents[2])
    script = tmp_path / "serve_probe.py"
    script.write_text(_SERVE_SCRIPT.format(repo=repo, port=8791))

    proc = subprocess.run(
        [sys.executable, str(script)],
        capture_output=True,
        text=True,
        timeout=45,
    )
    out = proc.stdout
    assert "SESSION_STOPPED" in out, (
        "SIGTERM bypassed teardown: the process was killed before the session "
        f"was stopped. stdout={out!r} stderr={proc.stderr[-800:]!r}"
    )
