"""Ported from v3 ``test_infra_stream_health.py`` — ReconnectingStreamBackend.

v4 ``ReconnectingStreamBackend`` is API-compatible with v3.  Health-check
tests are omitted because v4 has a different health API.
"""

from __future__ import annotations

from tradex_brokers.common.streaming import ReconnectingStreamBackend

# ---------------------------------------------------------------------------
# ReconnectingStreamBackend — subscribe / unsubscribe / reconnect
# ---------------------------------------------------------------------------


class _Backend:
    def __init__(self) -> None:
        self.subs: list[tuple[str, object]] = []
        self.unsubscribed: list[object] = []

    def subscribe_quotes(self, instruments: object, handler: object) -> object:
        self.subs.append(("quotes", handler))
        return "q-1"

    def subscribe_orders(self, handler: object) -> object:
        self.subs.append(("orders", handler))
        return "o-1"

    def subscribe_positions(self, handler: object) -> object:
        self.subs.append(("positions", handler))
        return "p-1"

    def unsubscribe(self, subscription: object) -> None:
        self.unsubscribed.append(subscription)

    def close(self) -> None: ...


def test_reconnect_restores_all_subscription_kinds() -> None:
    inner = _Backend()
    wrapper = ReconnectingStreamBackend(inner)
    wrapper.subscribe_quotes(["RELIANCE"], lambda q: None)
    wrapper.subscribe_orders(lambda o: None)
    wrapper.subscribe_positions(lambda p: None)
    assert len(inner.subs) == 3

    wrapper.reconnect()
    assert len(inner.unsubscribed) == 3
    assert len(inner.subs) == 6
    assert [kind for kind, _ in inner.subs] == [
        "quotes",
        "orders",
        "positions",
        "quotes",
        "orders",
        "positions",
    ]


def test_unsubscribe_removes_from_active_reconnect_pool() -> None:
    inner = _Backend()
    wrapper = ReconnectingStreamBackend(inner)
    sub = wrapper.subscribe_quotes(["RELIANCE"], lambda q: None)
    wrapper.subscribe_orders(lambda o: None)
    wrapper.unsubscribe(sub)
    assert len(inner.unsubscribed) == 1

    wrapper.reconnect()
    assert [kind for kind, _ in inner.subs] == ["quotes", "orders", "orders"]


def test_reconnect_suppresses_stale_handle_errors() -> None:
    class _FussyBackend:
        def __init__(self) -> None:
            self.subs: list[object] = []

        def subscribe_quotes(self, instruments: object, handler: object) -> object:
            self.subs.append(("quotes", handler))
            return "q-1"

        def subscribe_orders(self, handler: object) -> object:
            self.subs.append(("orders", handler))
            return "o-1"

        def unsubscribe(self, subscription: object) -> None:
            raise RuntimeError("stale handle")

    inner = _FussyBackend()
    wrapper = ReconnectingStreamBackend(inner)
    wrapper.subscribe_quotes(["RELIANCE"], lambda q: None)
    wrapper.subscribe_orders(lambda o: None)
    wrapper.reconnect()
    assert len(inner.subs) == 4


def test_reconnecting_stream_backend_resubscribes() -> None:
    class _SimpleBackend:
        def __init__(self) -> None:
            self.subs: list[tuple[str, object]] = []
            self.closed = False

        def subscribe_quotes(self, instruments: object, handler: object) -> object:
            self.subs.append(("quotes", handler))
            return "sub-1"

        def unsubscribe(self, subscription: object) -> None:
            self.subs.pop(0)

        def close(self) -> None:
            self.closed = True

    inner = _SimpleBackend()
    wrapper = ReconnectingStreamBackend(inner)
    wrapper.subscribe_quotes(["RELIANCE"], lambda q: None)
    assert len(inner.subs) == 1
    wrapper.reconnect()
    assert len(inner.subs) == 1
    wrapper.close()
    assert inner.closed is True
