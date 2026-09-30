"""The websocket test harness must fail, not hang, when no frame arrives.

Guard for the deadline installed in ``trading/tests/conftest.py``. Without it a
read with nothing behind it blocks the test thread until the outer pytest
timeout (120s by default), which is long enough that the run looks deadlocked
rather than failing — and that misread has already cost this repo twice.
"""

from __future__ import annotations

import time
from unittest.mock import MagicMock

import pytest

fastapi = pytest.importorskip("fastapi")

from fastapi.testclient import TestClient  # noqa: E402
from tradex_interfaces.fastapi_app import create_app  # noqa: E402
from tradex_reactive.bus import ReactiveBus  # noqa: E402


def _app() -> object:
    session = MagicMock()
    session.state = "READY"
    session.bus = ReactiveBus()
    return create_app(session=session)


def test_a_frame_that_never_arrives_fails_fast() -> None:
    """An unanswered read raises instead of blocking the test thread.

    Nothing is sent, and a fresh connection gets no unsolicited frame — the
    feed-status push stays silent on its first observation — so this read has
    nothing to return and must give up on its own deadline.
    """
    with TestClient(_app()).websocket_connect("/ws/stream") as ws:
        ws._receive_timeout = 0.25
        started = time.monotonic()
        with pytest.raises(TimeoutError) as excinfo:
            ws.receive_json()
        elapsed = time.monotonic() - started

    # The message names the deadline that fired, so this proves the per-test
    # value was honoured rather than the 5.0s default being reached — and it
    # does so without a wall-clock bound, which would be flaky on a loaded host.
    assert "0.25s" in str(excinfo.value), f"wrong deadline: {excinfo.value}"
    # Loose on purpose: this only needs to catch a fall back to the 120s
    # pytest timeout, not to measure the deadline.
    assert elapsed < 60, f"read took {elapsed:.1f}s — no deadline applied"


def test_the_deadline_does_not_clip_a_normal_ack() -> None:
    """A frame that does arrive is returned unchanged."""
    with TestClient(_app()).websocket_connect("/ws/stream") as ws:
        ws._receive_timeout = 5
        ws.send_json({"type": "stats"})
        frame = ws.receive_json()

    assert frame["type"] == "stats"
    assert "dropped" in frame


def test_an_unset_timeout_still_gets_a_deadline() -> None:
    """The default applies too, so every read is bounded, not just the ones
    that ask for it.

    The suites only set ``_receive_timeout`` where they expect to wait; the
    rest relied on a Starlette default that no longer exists. If Starlette ever
    reinstates the attribute this assertion fails, which is the signal that the
    patch below it may have become redundant.
    """
    with TestClient(_app()).websocket_connect("/ws/stream") as ws:
        assert not hasattr(ws, "_receive_timeout"), (
            "Starlette now defines _receive_timeout; re-check the patch in "
            "trading/tests/conftest.py"
        )
        started = time.monotonic()
        with pytest.raises(TimeoutError) as excinfo:
            ws.receive_json()
        elapsed = time.monotonic() - started

    assert "5.0s" in str(excinfo.value), f"unexpected message: {excinfo.value}"
    assert elapsed < 60, f"read took {elapsed:.1f}s — no deadline applied"
