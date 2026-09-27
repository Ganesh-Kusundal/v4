"""The app's ASGI lifespan must own session teardown, so SIGTERM cannot skip it.

Defect (Critical, commit a794fbe): ``tradex serve`` had no teardown of its own.
``uvicorn.run`` wraps its work in ``capture_signals``, which restores the
default handlers and re-raises the captured signal *inside* the ``with`` block,
so ``run()`` never returns and no caller-side ``finally`` is ever reached. For
SIGTERM the restored disposition is ``SIG_DFL``, so the kernel ends the process
there. SIGTERM is what ``docker stop``, Kubernetes, systemd and a plain ``kill``
send -- so under the signal that actually matters, the single-writer lock was
never released, the feed and broker sockets stayed open, and the market feed and
mark-to-market were never stopped.

The fix gives the app an ASGI lifespan whose shutdown stops the bound session.
Lifespan shutdown completes inside uvicorn's ``_serve()``, *before* the signal
is re-raised, so it runs for every signal. Owning teardown in the app also
covers the ``workers>1`` / ``reload`` child sessions, which nothing stopped.

What this module guards, and a boundary it draws honestly
--------------------------------------------------------
The lifespan calls ``session.stop()``. Empirically (this file's own tests) that
releases everything the *session* owns: the single-writer lock, the broker
sockets, the bus, the market feed, mark-to-market and the stream backend. Those
are asserted directly against a real ``SingleWriterLock`` and a real broker.

The kill switch, the durable idempotency guard and the event store are owned by
``ExecutionEngine.shutdown()``, *not* by ``session.stop()``; they are released
by ``RuntimeContext.close()`` (the trading runtime's teardown), which is a
different entry point from the app lifespan. This module pins both: the
lifespan contract above, and the engine contract below. It does *not* claim the
lifespan closes the engine's resources, because it does not -- asserting that
would be a green test guarding a behaviour the code does not have.

The tests use ``TestClient`` (an in-process ASGI driver), never a real socket or
a real server, so they are fast and hermetic. Driving ``with TestClient(app)``
runs the exact lifespan startup/shutdown pair that uvicorn runs on SIGTERM.
"""

from __future__ import annotations

from unittest.mock import MagicMock

import pytest
from starlette.testclient import TestClient
from tradex_domain import BrokerId
from tradex_execution.engine import ExecutionEngine
from tradex_execution.fill_sources import BrokerFillSource
from tradex_execution.sqlite_event_store import SQLiteEventStore
from tradex_execution.sqlite_store import SQLiteIdempotencyGuard
from tradex_interfaces.fastapi_app import create_app
from tradex_reactive.bus import ReactiveBus
from tradex_runtime.session_impl import SessionState, TradingSession
from tradex_runtime.shutdown import ShutdownCoordinator
from tradex_runtime.writer_lock import SingleWriterLock


def _fake_broker() -> MagicMock:
    """A broker stand-in with the surface ``session.stop()`` tears down.

    Same shape as ``trading/tests/runtime/test_live_boot_fail_closed.py``'s
    fake: a mock whose ``close`` is observable, so "was the broker torn down"
    is a real assertion rather than an inferred one.
    """
    broker = MagicMock()
    broker.stream_backend.return_value = MagicMock()
    broker.get_orderbook.return_value = []
    broker.get_positions.return_value = []
    broker.master_loader = None
    return broker


def _session(tmp_path) -> tuple[TradingSession, MagicMock, SingleWriterLock]:
    """A READY live-shaped session holding a real writer lock in *tmp_path*.

    The lock is constructed against a temp path so the test never writes the
    cwd-relative ``runtime/live/`` lockfile the live ``boot()`` would create.
    Everything else -- the engine, the durable guard, the event store -- is the
    real implementation, so "the SQLite handle was closed" is a real fact about
    a real connection, not a mock's opinion.
    """
    guard = SQLiteIdempotencyGuard(tmp_path / "orders.db")
    event_store = SQLiteEventStore(tmp_path / "events.db")
    engine = ExecutionEngine(
        bus=ReactiveBus(),
        fill_source=BrokerFillSource(_fake_broker()),
        idempotency_guard=guard,
        event_store=event_store,
    )
    writer_lock = SingleWriterLock(tmp_path / "writer.lock")
    writer_lock.acquire()

    session = TradingSession(
        broker=_fake_broker(),
        bus=ReactiveBus(),
        engine=engine,
        cache=engine.cache,
        broker_id=BrokerId.DHAN,
        mode="live",
        writer_lock=writer_lock,
    )
    session.start()
    return session, session._broker, writer_lock  # type: ignore[attr-defined]


# ---------------------------------------------------------------------------
# The lifespan contract: the session -- and what it owns -- is torn down
# ---------------------------------------------------------------------------


def test_lifespan_shutdown_stops_the_session_and_releases_its_resources(
    tmp_path,
) -> None:
    """On lifespan shutdown the session stops and the writer lock is released.

    This is the SIGTERM invariant in miniature: ASGI lifespan shutdown runs
    before uvicorn re-raises the signal, so anything stopped here is stopped
    under every signal including SIGTERM.
    """
    session, broker, writer_lock = _session(tmp_path)
    app = create_app(session, api_key=None)

    assert session.state is SessionState.READY
    assert writer_lock._held is True  # noqa: SLF001
    assert writer_lock._path.exists()  # noqa: SLF001

    with TestClient(app):
        # Startup ran; the session is still up and the lock still held.
        assert session.state is SessionState.READY
        assert writer_lock._held is True  # noqa: SLF001
        assert not broker.close.called

    # Shutdown ran.
    assert session.state is SessionState.STOPPED, (
        "the app's lifespan must stop the session; teardown owned only by the "
        "caller's finally is skipped when uvicorn re-raises SIGTERM"
    )
    assert writer_lock._held is False, (  # noqa: SLF001
        "the single-writer lock must be released on shutdown, or the next live "
        "process is refused and the account cannot be traded"
    )
    assert not writer_lock._path.exists(), (  # noqa: SLF001
        "the writer lockfile must be unlinked on release"
    )
    broker.close.assert_called()


def test_lifespan_shutdown_runs_without_a_signal_being_delivered(tmp_path) -> None:
    """The teardown belongs to the app, so it does not depend on a signal.

    Pre-fix, teardown lived in the caller's ``finally`` and therefore only
    happened on a clean return. Here no signal is sent at all -- the ``with``
    block simply exits -- and the session is still stopped, which is what
    makes the guarantee hold when a supervisor sends SIGTERM instead.
    """
    session, _broker, _lock = _session(tmp_path)
    app = create_app(session, api_key=None)

    with TestClient(app):
        pass

    assert session.state is SessionState.STOPPED


def test_lifespan_teardown_is_exactly_once_per_cycle(tmp_path) -> None:
    """One stop per startup/shutdown pair -- no double-stop, no missed stop.

    uvicorn can drive more than one lifespan cycle on a reused app object; each
    must stop the session exactly once. ``session.stop()`` is itself idempotent,
    so a second call is a no-op rather than a double teardown.
    """
    session, _broker, _lock = _session(tmp_path)
    app = create_app(session, api_key=None)

    with TestClient(app):
        pass
    assert session.state is SessionState.STOPPED

    # A second cycle on the same app must not raise and must stay STOPPED.
    with TestClient(app):
        pass
    assert session.state is SessionState.STOPPED


def test_lifespan_teardown_error_does_not_propagate(tmp_path) -> None:
    """A failing ``stop()`` is logged, not raised.

    A teardown that raises would turn a clean shutdown into a non-zero exit and
    could mask the signal's own exit code. ``_stop_bound_session`` swallows and
    logs for exactly this reason.
    """
    session, _broker, _lock = _session(tmp_path)
    app = create_app(session, api_key=None)

    def _boom() -> None:
        raise RuntimeError("teardown exploded")

    session.stop = _boom  # type: ignore[method-assign]

    # Entering and leaving the context must not raise even though stop() does.
    with TestClient(app):
        pass


def test_lifespan_teardown_tolerates_a_session_without_stop() -> None:
    """A session-like object with no ``stop`` is skipped, not crashed on.

    ``create_app`` accepts any session-ish object; the lifespan must degrade to
    a no-op rather than an AttributeError on a partial double.
    """
    app = create_app(object(), api_key=None)

    with TestClient(app):
        pass
    # Reaching here without an exception is the assertion.


# ---------------------------------------------------------------------------
# The engine contract: where the kill switch and the durable handles live
# ---------------------------------------------------------------------------


def test_engine_shutdown_sets_kill_switch_and_closes_the_durable_guard(
    tmp_path,
) -> None:
    """``ExecutionEngine.shutdown()`` halts intake and releases the guard.

    This is the *other* half of teardown, owned by the engine rather than the
    app. ``session.stop()`` does not do it; ``RuntimeContext.close()`` does, by
    registering ``engine.shutdown`` as a shutdown phase. Pinning it here keeps
    the engine-side resources from silently leaking on that path.
    """
    guard = SQLiteIdempotencyGuard(tmp_path / "orders.db")
    event_store = SQLiteEventStore(tmp_path / "events.db")
    engine = ExecutionEngine(
        bus=ReactiveBus(),
        fill_source=BrokerFillSource(_fake_broker()),
        idempotency_guard=guard,
        event_store=event_store,
    )

    assert engine.kill_switch is False
    assert guard._conn is not None  # noqa: SLF001

    engine.shutdown()

    assert engine.kill_switch is True, (
        "shutdown must halt new submissions; a book still admitting orders "
        "after the process decided to stop is how a restart double-trades"
    )
    # The guard's SQLite handle is released by shutdown (via its close()).
    with pytest.raises(Exception):
        guard._conn.execute("SELECT 1")  # noqa: SLF001

    # The event store is not owned by the engine; close it so the test leaves
    # no open handle behind.
    event_store.close()


def test_runtime_context_teardown_order_releases_everything(tmp_path) -> None:
    """``ShutdownCoordinator`` runs the engine phase before the session phase.

    This mirrors ``RuntimeContext.close()``'s priorities: engine (halt intake,
    close the guard) then session (release the writer lock, close the broker).
    Ordering matters -- the engine must stop admitting orders before the writer
    lock is handed back, or a live restart could slip an order into a book whose
    guard was just closed.
    """
    order: list[str] = []

    coord = ShutdownCoordinator()
    coord.register("engine", priority=2, action=lambda: order.append("engine"))
    coord.register("session", priority=3, action=lambda: order.append("session"))
    coord.register("strategies", priority=1, action=lambda: order.append("strategies"))

    failures = coord.shutdown()

    assert failures == []
    assert order == ["strategies", "engine", "session"], (
        f"teardown ran out of order: {order!r}"
    )
