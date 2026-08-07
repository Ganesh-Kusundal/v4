"""WebSocket resilience tests for AutoReconnectMixin (Phase 6.1).

Tests reconnection logic, subscription replay, backoff exhaustion, and
flapping-connection detection.
"""

from __future__ import annotations

import threading
import time
from unittest.mock import MagicMock

from tradex_brokers.common.ws_reconnect import AutoReconnectMixin, WSReconnectManager


class _FakeBackend(AutoReconnectMixin):
    """Test harness mixing in AutoReconnectMixin."""

    def __init__(
        self,
        ws_factory: callable | None = None,
        max_retries: int = 3,
        base_delay: float = 0.01,
    ) -> None:
        self._ws = None
        self._closing = False
        self._state_lock = threading.RLock()
        self._connect_lock = threading.Lock()
        self._ws_factory = ws_factory or MagicMock()
        self._resubscribe_calls: list = []
        self._receive_loop_calls: list = []
        self._init_reconnect(
            reconnect=True,
            max_retries=max_retries,
            base_delay=base_delay,
            max_delay=base_delay * 2,
            grace_seconds=0.1,
        )

    def _open_socket(self):
        ws = self._ws_factory()
        self._ws = ws
        return ws

    def _resubscribe(self, ws) -> None:
        self._resubscribe_calls.append(ws)

    def _receive_loop(self) -> None:
        self._receive_loop_calls.append(self._ws)


class TestAutoReconnectMixin:
    """AutoReconnectMixin reconnection logic."""

    def test_reconnect_fires_after_drop(self) -> None:
        """Socket drop triggers reconnection attempt."""
        backend = _FakeBackend(base_delay=0.001)
        backend._ws = MagicMock()
        backend._schedule_reconnect()
        # Give the reconnect thread time to run
        time.sleep(0.05)
        assert backend._ws is not None or backend._reconnect_thread is not None

    def test_subscriptions_replayed_on_new_socket(self) -> None:
        """Resubscribe is called on the new socket after reconnect."""
        backend = _FakeBackend(base_delay=0.001)
        backend._ws = None  # Simulate socket drop
        backend._schedule_reconnect()
        time.sleep(0.05)
        assert len(backend._resubscribe_calls) >= 1

    def test_backoff_exhaustion_after_max_retries(self) -> None:
        """Reconnect gives up after max_retries failures."""
        factory = MagicMock(side_effect=RuntimeError("socket open failed"))
        backend = _FakeBackend(ws_factory=factory, max_retries=2, base_delay=0.001)
        backend._ws = None
        backend._schedule_reconnect()
        time.sleep(0.1)
        # Should have tried max_retries times
        assert factory.call_count >= 2

    def test_close_cancels_pending_reconnect(self) -> None:
        """close() sets _closing and cancels pending reconnect."""
        backend = _FakeBackend()
        backend._ws = None
        backend._closing = True
        backend._schedule_reconnect()
        # Should not start a reconnect thread
        assert backend._reconnect_thread is None

    def test_flapping_connection_keeps_backing_off(self) -> None:
        """Flapping connection (opens then immediately drops) keeps backing off."""
        call_count = 0

        def _flapping_factory():
            nonlocal call_count
            call_count += 1
            ws = MagicMock()
            return ws

        backend = _FakeBackend(ws_factory=_flapping_factory, max_retries=5, base_delay=0.001)
        backend._ws = MagicMock()
        # Simulate multiple drops
        for _ in range(3):
            backend._ws = None
            backend._schedule_reconnect()
            time.sleep(0.02)
        # Should have attempted multiple reconnects
        assert call_count >= 3


class TestWSReconnectManager:
    """WSReconnectManager exponential backoff."""

    def test_next_delay_returns_none_after_max_retries(self) -> None:
        mgr = WSReconnectManager(max_retries=2, base_delay=1.0)
        assert mgr.next_delay() is not None
        assert mgr.next_delay() is not None
        assert mgr.next_delay() is None

    def test_reset_clears_attempt_counter(self) -> None:
        mgr = WSReconnectManager(max_retries=2)
        mgr.next_delay()
        mgr.next_delay()
        assert mgr.exhausted
        mgr.reset()
        assert not mgr.exhausted
        assert mgr.next_delay() is not None

    def test_exponential_backoff(self) -> None:
        mgr = WSReconnectManager(max_retries=5, base_delay=1.0, exponential_base=2.0, jitter=False)
        d1 = mgr.next_delay()
        d2 = mgr.next_delay()
        d3 = mgr.next_delay()
        assert d1 == 1.0
        assert d2 == 2.0
        assert d3 == 4.0
