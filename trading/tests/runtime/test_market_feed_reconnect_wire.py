"""Wave B0: MarketFeed → broker reconnect hook wiring tests.

Verifies that:
1. MarketFeed.subscribe() registers notify_reconnect on the broker.
2. When the broker fires the hook (simulating a socket reconnect), the
   supervisor transitions to RESYNCHRONIZING (not READY).
3. The hook is wired for both quote backends (ws) and depth backends.
4. Brokers without set_reconnect_hook (paper/fakes) are handled safely.
5. The hook is idempotent — repeated subscribe calls don't double-fire.

These are unit tests using thin fakes; no real WebSocket connections.
"""
from __future__ import annotations

from datetime import UTC, datetime
from typing import Any

from tradex_domain.instruments import Equity, Instrument

from tradex_trading.reactive.bus import ReactiveBus
from tradex_trading.runtime.feed_supervisor import FeedState, FeedSupervisor
from tradex_trading.runtime.market_feed import MarketFeed

# ---------------------------------------------------------------------------
# Fakes
# ---------------------------------------------------------------------------


class _HookCapableBroker:
    """Fake broker that records set_reconnect_hook calls and can fire the hook."""

    def __init__(self, *, has_depth_30: bool = False) -> None:
        self.has_depth_30 = has_depth_30
        self._reconnect_hook: Any = None
        self.quote_subs: list[Any] = []
        self.depth_subs: list[Any] = []

    def subscribe_quotes(self, instruments: list[Instrument], handler: Any) -> str:
        self.quote_subs.append(instruments)
        return f"q-{id(handler)}"

    def subscribe_depth(self, instruments: list[Instrument], handler: Any) -> str:
        self.depth_subs.append(instruments)
        return f"d-{id(handler)}"

    def __getattr__(self, name: str) -> Any:
        if name == "subscribe_depth_30" and self.has_depth_30:
            return self.subscribe_depth
        raise AttributeError(name)

    def unsubscribe(self, sub: object) -> None:
        pass

    def unsubscribe_instruments(self, instruments: object) -> None:
        pass

    def set_reconnect_hook(self, cb: Any) -> None:
        self._reconnect_hook = cb

    def fire_reconnect(self) -> None:
        """Simulate a successful socket reconnect firing the registered hook."""
        if self._reconnect_hook is not None:
            self._reconnect_hook()


class _NoHookBroker:
    """Fake broker that does NOT expose set_reconnect_hook (paper-like)."""

    def __init__(self) -> None:
        self.quote_subs: list[Any] = []

    def subscribe_quotes(self, instruments: list[Instrument], handler: Any) -> str:
        self.quote_subs.append(instruments)
        return f"q-{id(handler)}"

    def unsubscribe(self, sub: object) -> None:
        pass

    def unsubscribe_instruments(self, instruments: object) -> None:
        pass


def _inst() -> Instrument:
    return Equity.of("NSE", "RELIANCE")


def _feed(broker: Any) -> MarketFeed:
    return MarketFeed(broker=broker, bus=ReactiveBus())


# ---------------------------------------------------------------------------
# Tests: hook registration
# ---------------------------------------------------------------------------


class TestHookRegistration:
    """subscribe() wires notify_reconnect onto the broker's reconnect hook."""

    def test_subscribe_registers_hook(self) -> None:
        broker = _HookCapableBroker()
        feed = _feed(broker)

        assert broker._reconnect_hook is None, "hook must not be set before subscribe"
        feed.subscribe([_inst()])
        # Use the stable cached reference (same reasoning as _quote_cb / _depth_cb)
        assert broker._reconnect_hook is feed._reconnect_cb, (
            "subscribe must register the stable _reconnect_cb on the broker"
        )

    def test_start_registers_hook(self) -> None:
        """start() internally calls subscribe(), so the hook is wired."""
        broker = _HookCapableBroker()
        feed = _feed(broker)

        feed.start([_inst()])
        assert broker._reconnect_hook is feed._reconnect_cb

    def test_hook_registered_after_depth_subscribe(self) -> None:
        """Depth subscribe also registers the hook (depth backend may differ)."""
        broker = _HookCapableBroker()
        feed = _feed(broker)

        feed.subscribe([_inst()], depth="20")
        assert broker._reconnect_hook is feed._reconnect_cb

    def test_no_hook_broker_does_not_raise(self) -> None:
        """Brokers without set_reconnect_hook are handled silently."""
        broker = _NoHookBroker()
        feed = _feed(broker)
        feed.subscribe([_inst()])  # must not raise
        assert feed.active is True

    def test_hook_registration_is_idempotent(self) -> None:
        """Subscribing twice sets the same hook; it must fire once on reconnect."""
        broker = _HookCapableBroker()
        feed = _feed(broker)

        feed.subscribe([_inst()])
        first_hook = broker._reconnect_hook
        feed.subscribe([Equity.of("NSE", "TCS")])
        assert broker._reconnect_hook is first_hook is feed._reconnect_cb


# ---------------------------------------------------------------------------
# Tests: supervisor state on simulated reconnect
# ---------------------------------------------------------------------------


class TestReconnectSupervisorTransition:
    """Firing the hook must move supervisor to RESYNCHRONIZING, not READY."""

    def _reach_ready(self, supervisor: Any) -> None:
        """Drive supervisor to READY via the canonical recovery cycle."""
        supervisor.connected()
        supervisor.recovery_started()
        supervisor.recovery_succeeded(
            last_event_at=datetime(2026, 9, 24, 10, 0, tzinfo=UTC),
            recovered_through=datetime(2026, 9, 24, 10, 1, tzinfo=UTC),
        )

    def test_hook_fires_resynchronizing_transition(self) -> None:
        broker = _HookCapableBroker()
        feed = _feed(broker)
        feed.subscribe([_inst()])

        supervisor = feed.supervisor
        self._reach_ready(supervisor)
        assert supervisor.state is FeedState.READY

        broker.fire_reconnect()
        assert supervisor.state is FeedState.RESYNCHRONIZING, (
            "reconnect must demote supervisor to RESYNCHRONIZING, not leave it READY"
        )

    def test_reconnect_triggers_recovery_coordinator(self) -> None:
        """Broker reconnect must run history recovery — socket alone must not READY."""

        class _SpyRecovery:
            calls = 0

            def recover(self) -> object:
                self.calls += 1
                return object()

        broker = _HookCapableBroker()
        feed = _feed(broker)
        recovery = _SpyRecovery()
        feed.set_recovery(recovery)
        feed.subscribe([_inst()])

        self._reach_ready(feed.supervisor)

        broker.fire_reconnect()
        # Daemon thread may need a beat to start.
        import time

        deadline = time.monotonic() + 2.0
        while recovery.calls == 0 and time.monotonic() < deadline:
            time.sleep(0.01)
        assert recovery.calls >= 1, (
            "broker reconnect must trigger the fail-closed recovery coordinator"
        )
        assert feed.supervisor.state is not FeedState.READY

    def test_socket_alone_never_ready(self) -> None:
        """Core invariant: after reconnect the supervisor can never be READY
        without an explicit recovery cycle.
        """
        broker = _HookCapableBroker()
        feed = _feed(broker)
        feed.subscribe([_inst()])

        self._reach_ready(feed.supervisor)
        assert feed.supervisor.state is FeedState.READY

        broker.fire_reconnect()
        assert feed.supervisor.state is not FeedState.READY, (
            "socket reconnect alone must never leave supervisor READY"
        )

    def test_supervisor_starts_resynchronizing_not_ready_after_reconnect(self) -> None:
        """Reconnect on a never-ready feed: supervisor goes RESYNCHRONIZING."""
        broker = _HookCapableBroker()
        supervisor = FeedSupervisor()
        feed = MarketFeed(broker=broker, bus=ReactiveBus(), supervisor=supervisor)
        feed.subscribe([_inst()])

        broker.fire_reconnect()
        assert supervisor.state is FeedState.RESYNCHRONIZING


# ---------------------------------------------------------------------------
# Tests: BaseBroker.set_reconnect_hook fanout
# ---------------------------------------------------------------------------


class TestBaseBrokerFanout:
    """BaseBroker.set_reconnect_hook propagates to both market backends."""

    def _make_dhan_broker_with_fake_backends(self) -> tuple[Any, Any, Any]:
        """Build a DhanBroker (no transport) and attach two fake stream backends.

        Returns (broker, ws_backend, depth_backend).
        """
        from tradex_brokers.dhan.adapter import DhanBroker

        class _FakeStreamBackend:
            def __init__(self, name: str) -> None:
                self.name = name
                self.hook: Any = None

            def set_reconnect_hook(self, cb: Any) -> None:
                self.hook = cb

            def close(self) -> None:
                pass

        broker = DhanBroker()
        ws_backend = _FakeStreamBackend("ws")
        depth_backend = _FakeStreamBackend("depth")
        broker._ws_backend = ws_backend
        broker._depth_backend = depth_backend
        return broker, ws_backend, depth_backend

    def test_fanout_to_ws_and_depth_backends(self) -> None:
        """Both _ws_backend and _depth_backend receive the hook."""
        broker, ws_backend, depth_backend = self._make_dhan_broker_with_fake_backends()

        cb = lambda: None
        broker.set_reconnect_hook(cb)
        assert ws_backend.hook is cb, "_ws_backend must receive the hook"
        assert depth_backend.hook is cb, "_depth_backend must receive the hook"

    def test_fanout_skips_none_backends(self) -> None:
        """Fanout with no backends attached must not raise."""
        from tradex_brokers.dhan.adapter import DhanBroker

        broker = DhanBroker()
        # _ws_backend and _depth_backend are None by default
        assert broker._ws_backend is None
        assert broker._depth_backend is None

        broker.set_reconnect_hook(lambda: None)  # must not raise

    def test_order_backend_does_not_receive_hook(self) -> None:
        """Order stream backend must NOT get the reconnect hook."""
        from tradex_brokers.dhan.adapter import DhanBroker

        class _TrackingBackend:
            def __init__(self) -> None:
                self.hook_calls = 0

            def set_reconnect_hook(self, cb: Any) -> None:
                self.hook_calls += 1

            def close(self) -> None:
                pass

        broker = DhanBroker()
        order_backend = _TrackingBackend()
        broker._order_backend = order_backend
        # No ws or depth backends
        assert broker._ws_backend is None
        assert broker._depth_backend is None

        broker.set_reconnect_hook(lambda: None)
        assert order_backend.hook_calls == 0, (
            "order backend must NOT receive the reconnect hook"
        )
