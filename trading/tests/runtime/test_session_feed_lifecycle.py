"""TradingSession ownership of the stale-feed monitor and recovery hook.

The session must start the owned stale poll when it goes live, stop it on
``stop()`` exactly once, and expose the fail-closed recovery coordinator
without ever running a bare-reconnect recovery implicitly.
"""

from __future__ import annotations

from unittest.mock import MagicMock

import pytest
from tradex_domain import BrokerId
from tradex_domain.errors import CapabilityNotSupportedError

from tradex_trading.reactive.bus import ReactiveBus
from tradex_trading.sdk.session import SessionState, TradingSession


class _FakeMonitor:
    def __init__(self) -> None:
        self.starts = 0
        self.stops = 0

    def start(self) -> None:
        self.starts += 1

    def stop(self) -> None:
        self.stops += 1


class _FakeRecovery:
    def __init__(self, outcome: object) -> None:
        self.outcome = outcome
        self.calls = 0

    def recover(self) -> object:
        self.calls += 1
        return self.outcome


def _session(**kwargs: object) -> TradingSession:
    return TradingSession(
        broker=MagicMock(),
        bus=ReactiveBus(),
        engine=MagicMock(),
        cache=MagicMock(),
        broker_id=BrokerId.PAPER,
        **kwargs,
    )


def test_start_starts_the_owned_monitor_once() -> None:
    monitor = _FakeMonitor()
    session = _session(feed_monitor=monitor)

    session.start()
    session.start()  # READY -> no-op

    assert session.state is SessionState.READY
    assert monitor.starts == 1


def test_stop_stops_the_monitor_and_releases_it() -> None:
    monitor = _FakeMonitor()
    session = _session(feed_monitor=monitor)
    session.start()

    session.stop()

    assert monitor.stops == 1
    assert session.feed_monitor is None
    assert session.state is SessionState.STOPPED
    session.stop()  # idempotent
    assert monitor.stops == 1


def test_session_without_monitor_still_stops_cleanly() -> None:
    session = _session()

    session.start()
    session.stop()

    assert session.feed_monitor is None
    assert session.state is SessionState.STOPPED


def test_recover_feed_without_coordinator_raises() -> None:
    session = _session()
    session.start()
    try:
        with pytest.raises(CapabilityNotSupportedError, match="recovery coordinator"):
            session.recover_feed()
    finally:
        session.stop()


def test_recover_feed_delegates_to_the_coordinator() -> None:
    outcome = object()
    recovery = _FakeRecovery(outcome)
    session = _session(feed_recovery=recovery)
    session.start()

    try:
        assert session.feed_recovery is recovery
        assert session.recover_feed() is outcome
        assert recovery.calls == 1
    finally:
        session.stop()


def test_recover_feed_is_never_implicit_on_start() -> None:
    recovery = _FakeRecovery(object())
    session = _session(feed_recovery=recovery)

    session.start()
    try:
        assert recovery.calls == 0  # a reconnect must be an explicit decision
    finally:
        session.stop()
