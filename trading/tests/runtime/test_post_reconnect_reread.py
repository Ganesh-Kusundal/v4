"""Lane 3b / AC-12: post-reconnect venue re-read + indefinite backoff.

Two halves of the same lane:

1. **Re-read, not resume blind.** The order/portfolio stream carries only
   *incremental* updates, so anything that changed at the venue while the
   socket was down is never re-delivered. After every reconnect the session
   must re-read orders *and* positions from the venue and reconcile — through
   the existing ``_run_startup_reconciliation`` path, not a second one.

2. **Indefinite backoff.** A dropped socket is retried forever; the delay
   holds at the base, then escalates to a ceiling. There is no attempt count
   at which the stream is silently abandoned for the rest of the session.
"""
from __future__ import annotations

import threading
import time
from unittest.mock import MagicMock

import tradex_runtime.startup as startup_mod
from tradex_brokers.common.ws_reconnect import AutoReconnectMixin, WSReconnectManager
from tradex_execution.engine import ExecutionEngine
from tradex_execution.fill_sources import SimulatedFillSource
from tradex_reactive.bus import ReactiveBus


def _engine() -> ExecutionEngine:
    return ExecutionEngine(
        bus=ReactiveBus(),
        fill_source=SimulatedFillSource(),
    )


class _HookBackend:
    """Minimal order-stream backend exposing only the reconnect-hook seam."""

    def __init__(self) -> None:
        self.hook = None
        self.hook_calls = 0

    def set_reconnect_hook(self, cb) -> None:
        self.hook = cb

    def fire(self) -> None:
        """Simulate a *successful* socket reconnect (the mixin fires the hook)."""
        assert self.hook is not None, "no reconnect hook was installed"
        self.hook_calls += 1
        self.hook()


# ---------------------------------------------------------------------------
# 1. A reconnect re-reads orders AND positions from the venue
# ---------------------------------------------------------------------------


class TestReconnectTriggersVenueReread:
    """A reconnect pulls the venue book; both orders and positions."""

    def test_reconnect_rereads_orders_and_positions(self) -> None:
        broker = MagicMock()
        broker.get_orderbook.return_value = []
        broker.get_positions.return_value = []
        engine = _engine()
        backend = _HookBackend()

        startup_mod._install_post_reconnect_reread(broker, engine, backend)
        assert backend.hook is not None, "no hook installed on the order backend"

        backend.fire()

        broker.get_orderbook.assert_called_once()
        broker.get_positions.assert_called_once()

    def test_reread_runs_the_shared_reconciliation(self) -> None:
        """The re-read is the startup reconciliation, not a parallel path."""
        broker = MagicMock()
        broker.get_orderbook.return_value = []
        broker.get_positions.return_value = []
        engine = _engine()
        calls: list[tuple] = []
        real = startup_mod._run_startup_reconciliation

        def _spy(b, e):
            calls.append((b, e))
            return real(b, e)

        startup_mod._run_startup_reconciliation = _spy  # type: ignore[assignment]
        try:
            backend = _HookBackend()
            startup_mod._install_post_reconnect_reread(broker, engine, backend)
            backend.fire()
        finally:
            startup_mod._run_startup_reconciliation = real  # type: ignore[assignment]

        assert calls == [(broker, engine)], (
            "the post-reconnect path must reuse _run_startup_reconciliation"
        )

    def test_each_reconnect_rereads_again(self) -> None:
        """Every reconnect re-reads — a second drop is covered too."""
        broker = MagicMock()
        broker.get_orderbook.return_value = []
        broker.get_positions.return_value = []
        engine = _engine()
        backend = _HookBackend()
        startup_mod._install_post_reconnect_reread(broker, engine, backend)

        backend.fire()
        backend.fire()
        backend.fire()

        assert broker.get_orderbook.call_count == 3
        assert broker.get_positions.call_count == 3

    def test_no_backend_is_a_noop(self) -> None:
        """A broker with no order backend does not explode at boot."""
        engine = _engine()
        startup_mod._install_post_reconnect_reread(MagicMock(), engine, None)
        no_hook = object()  # backend without set_reconnect_hook
        startup_mod._install_post_reconnect_reread(MagicMock(), engine, no_hook)


# ---------------------------------------------------------------------------
# 2. The re-read reconciles: drift is surfaced, not silently swallowed
# ---------------------------------------------------------------------------


class TestRereadReconcilesDrift:
    """A drifted order is surfaced, and a critical drift trips the kill switch."""

    def test_drifted_order_surfaced_and_kill_switch_trips(self) -> None:
        from tradex_execution.reconciliation import DriftItem, DriftSeverity

        engine = _engine()
        drift = DriftItem(
            kind="order",
            key="ORDER-1",
            severity=DriftSeverity.HIGH,
            reason="venue filled, local says open",
        )
        broker = MagicMock()
        broker.get_orderbook.return_value = []
        broker.get_positions.return_value = []
        # Force the reconciliation to observe the drift.
        engine.reconcile = MagicMock(return_value=[drift])  # type: ignore[method-assign]
        backend = _HookBackend()

        startup_mod._install_post_reconnect_reread(broker, engine, backend)
        backend.fire()

        # The drift is passed to engine.reconcile and, being HIGH, halts trading.
        engine.reconcile.assert_called_once()
        assert engine.kill_switch is True, (
            "a HIGH drift found by the post-reconnect re-read must trip the "
            "kill switch rather than be absorbed"
        )

    def test_reconciled_book_refreshes_order_status(self) -> None:
        """A re-read with a matching book performs no kill-switch trip."""
        broker = MagicMock()
        broker.get_orderbook.return_value = []
        broker.get_positions.return_value = []
        engine = _engine()
        backend = _HookBackend()

        startup_mod._install_post_reconnect_reread(broker, engine, backend)
        backend.fire()

        assert engine.kill_switch is False, (
            "a clean re-read must not halt a healthy session"
        )

    def test_hook_exception_does_not_escape(self) -> None:
        """A failing re-read is contained so the receive loop survives."""
        broker = MagicMock()
        broker.get_orderbook.side_effect = RuntimeError("venue down")
        broker.get_positions.side_effect = RuntimeError("venue down")
        engine = _engine()
        backend = _HookBackend()
        startup_mod._install_post_reconnect_reread(broker, engine, backend)

        # Must not raise out of the hook (the mixin would only log it, but a
        # bare `_run_startup_reconciliation` already degrades; the guard is
        # belt-and-braces for a reconcile that raises unexpectedly).
        backend.fire()  # no exception escapes


# ---------------------------------------------------------------------------
# 3. NEGATIVE CONTROL: a socket that never dropped does NOT re-read
# ---------------------------------------------------------------------------


class TestNoRereadWithoutAReconnect:
    """Installing the hook alone performs no re-read."""

    def test_install_does_not_reread(self) -> None:
        broker = MagicMock()
        engine = _engine()
        backend = _HookBackend()

        startup_mod._install_post_reconnect_reread(broker, engine, backend)

        broker.get_orderbook.assert_not_called()
        broker.get_positions.assert_not_called()
        assert backend.hook_calls == 0

    def test_a_live_socket_never_fires_the_hook(self) -> None:
        """The real backend only fires the hook on a reconnect, not on connect."""
        reconnects: list[int] = []

        class _Backend(AutoReconnectMixin):
            """Mixin backend that never drops, so the hook must never fire."""

            def __init__(self) -> None:
                self._ws = object()  # a live socket, never cleared
                self._closing = False
                self._state_lock = threading.RLock()
                self._connect_lock = threading.Lock()
                self._ws_factory = MagicMock()
                self._init_reconnect(reconnect=True, base_delay=0.001)

            def _open_socket(self):
                return MagicMock()

            def _resubscribe(self, ws) -> None:
                pass

            def _receive_loop(self) -> None:
                pass

        backend = _Backend()
        backend.set_reconnect_hook(lambda: reconnects.append(1))
        # No drop → _schedule_reconnect is never called.
        time.sleep(0.05)
        assert reconnects == [], (
            "a connection that never dropped must not trigger a re-read"
        )


# ---------------------------------------------------------------------------
# 4. Backoff: base delay, escalation, 300s ceiling, and INDEFINITE retries
# ---------------------------------------------------------------------------


class TestBackoffSchedule:
    """The lane's delay schedule and its refusal to ever give up."""

    def test_default_schedule_is_5s_to_300s_ceiling(self) -> None:
        mgr = WSReconnectManager(jitter=False)
        # Attempts 1..9: base delay.
        early = [mgr.next_delay() for _ in range(9)]
        assert early == [5.0] * 9, f"attempts 1..9 should hold the 5s base, got {early}"
        # Attempt 10 onward escalates toward the ceiling.
        later = [mgr.next_delay() for _ in range(12)]
        assert later[-1] == 300.0, f"the delay must reach the 300s ceiling, got {later}"
        assert later == sorted(later), f"the delay must escalate monotonically, got {later}"

    def test_delay_never_exceeds_ceiling(self) -> None:
        mgr = WSReconnectManager(jitter=False)
        for _ in range(100):
            assert mgr.next_delay() <= 300.0

    def test_retries_continue_well_past_ten_attempts(self) -> None:
        """NEGATIVE CONTROL: retrying must not stop after N attempts."""
        mgr = WSReconnectManager(jitter=False)
        # 40 attempts — far past the 10th — every one must return a delay.
        for _ in range(40):
            assert mgr.next_delay() is not None
        assert mgr.attempt_count == 40

    def test_ceiling_holds_forever(self) -> None:
        mgr = WSReconnectManager(jitter=False)
        for _ in range(60):
            mgr.next_delay()
        assert mgr.next_delay() == 300.0
        assert mgr.next_delay() == 300.0

    def test_very_long_outage_does_not_overflow(self) -> None:
        """A multi-day outage must not overflow the escalation exponent.

        Retries are indefinite, so the attempt counter is unbounded. Capping
        the *delay* after computing ``2 ** over`` would still evaluate the
        power, and ``2 ** 2000`` raises OverflowError — which would kill the
        reconnect thread and leave the socket dead, i.e. the exact silent
        death this lane exists to prevent.
        """
        mgr = WSReconnectManager(jitter=False)
        for _ in range(60):  # ramp past the escalation point to the ceiling
            mgr.next_delay()
        for _ in range(3000):  # a very long outage
            assert mgr.next_delay() == 300.0


class TestMixinRetriesIndefinitely:
    """The mixin keeps scheduling attempts; it never abandons the socket."""

    class _AlwaysFails(AutoReconnectMixin):
        def __init__(self, base_delay: float = 0.001) -> None:
            self._ws = None
            self._closing = False
            self._state_lock = threading.RLock()
            self._connect_lock = threading.Lock()
            self._attempts = 0
            self._init_reconnect(
                reconnect=True, max_retries=2, base_delay=base_delay,
                max_delay=base_delay * 4,
            )

        def _open_socket(self):
            self._attempts += 1
            raise RuntimeError("socket open failed")

        def _resubscribe(self, ws) -> None:
            pass

        def _receive_loop(self) -> None:
            pass

    def test_socket_keeps_being_reopened_past_max_retries(self) -> None:
        """Retries continue past max_retries; only close() stops them.

        Runs a real reconnect thread, so it is bounded generously and then
        stopped explicitly (via ``_closing``) so it cannot leak into later
        tests. The assertion is deliberately "well past max_retries", not a
        throughput target, so it is insensitive to machine load.
        """
        backend = TestMixinRetriesIndefinitely._AlwaysFails()
        try:
            backend._schedule_reconnect()
            # max_retries=2; 5 attempts is already unambiguously past it.
            deadline = time.monotonic() + 10
            while backend._attempts < 5 and time.monotonic() < deadline:
                time.sleep(0.005)
            assert backend._attempts >= 5, (
                f"only {backend._attempts} attempts; a dropped socket must be "
                "retried indefinitely, not abandoned after max_retries"
            )
        finally:
            backend._closing = True  # stop the retry thread deterministically

    def test_close_still_cancels_retrying(self) -> None:
        """close() is the only way to stop — the operator's, not the clock's."""
        backend = TestMixinRetriesIndefinitely._AlwaysFails()
        backend._closing = True
        backend._schedule_reconnect()
        time.sleep(0.05)
        assert backend._reconnect_thread is None
        assert backend._attempts == 0
