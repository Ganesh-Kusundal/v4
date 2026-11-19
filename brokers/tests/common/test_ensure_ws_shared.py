"""One ``_ensure_ws`` for the market-data and depth streams (was three copies)."""

import threading

from tradex_brokers.common.ws_reconnect import AutoReconnectMixin, WSReconnectManager
from tradex_brokers.dhan.ws_streams import DhanDepthStreamBackend, DhanMarketDataStreamBackend
from tradex_brokers.upstox.ws_streams import UpstoxMarketDataStreamBackend


def test_streams_use_the_mixin_ensure_ws():
    for cls in (DhanMarketDataStreamBackend, DhanDepthStreamBackend, UpstoxMarketDataStreamBackend):
        assert cls._ensure_ws is AutoReconnectMixin._ensure_ws, cls.__name__


class _Ws:
    def recv(self):  # presence of recv starts the receive loop
        return None


class _Backend(AutoReconnectMixin):
    def __init__(self, resubscribe_raises=False):
        self._ws = None
        self._closing = False
        self._state_lock = threading.RLock()
        self._connect_lock = threading.Lock()
        self._ws_factory = object()
        self._reconnect_thread = None
        self._reconnect = WSReconnectManager()
        self._last_success_at = 0.0
        self.calls = []
        self._raises = resubscribe_raises
        self.looped = threading.Event()

    def _open_socket(self):
        self.calls.append("open")
        return _Ws()

    def _resubscribe(self, ws):
        self.calls.append("resubscribe")
        if self._raises:
            raise RuntimeError("boom")

    def _receive_loop(self):
        self.looped.set()


def test_opens_replays_then_starts_loop_once():
    b = _Backend()
    b._ensure_ws()
    b._ensure_ws()  # already connected: no second open
    assert b.looped.wait(2)
    assert b.calls == ["open", "resubscribe"]


def test_resubscribe_failure_does_not_abort_open():
    b = _Backend(resubscribe_raises=True)
    b._ensure_ws()
    assert b._ws is not None and b.looped.wait(2)


def test_no_open_when_closing_or_reconnect_pending():
    b = _Backend()
    b._closing = True
    b._ensure_ws()
    b._closing = False
    b._reconnect_thread = threading.Thread()
    b._ensure_ws()
    assert b.calls == [] and b._ws is None
