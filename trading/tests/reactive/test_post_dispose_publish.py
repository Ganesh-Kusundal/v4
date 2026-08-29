"""M3 — post-dispose publish is a silent no-op.

The principal-architect review (M3) found that ``ReactiveBus.publish``
after ``dispose()`` silently enqueues to a completed ``Subject`` —
no error, no warning, the message is lost. Fix: ``publish()`` on
a disposed bus must raise ``RuntimeError`` so the caller knows
their message never reached any subscriber.

Scope:
- ``ReactiveBus.publish`` after ``dispose()`` raises ``RuntimeError``.
- Existing tests that publish after dispose (if any) must be
  updated to either reset the bus or stop publishing.
"""

from __future__ import annotations

import pytest

from tradex_trading.reactive.bus import ReactiveBus


def test_publish_after_dispose_raises() -> None:
    """RED: publish() on a disposed bus raises RuntimeError."""
    bus = ReactiveBus()
    bus.dispose()
    with pytest.raises(RuntimeError, match="disposed"):
        bus.publish("after-dispose")


def test_publish_before_dispose_still_works() -> None:
    """Backward compat: publish() before dispose() works as before."""
    bus = ReactiveBus()
    received: list[str] = []
    bus.subscribe(on_next=received.append)
    bus.publish("ok")
    assert received == ["ok"]


def test_dispose_is_idempotent() -> None:
    """Double dispose must not raise; subsequent publish still raises."""
    bus = ReactiveBus()
    bus.dispose()
    bus.dispose()  # second dispose is a no-op
    with pytest.raises(RuntimeError, match="disposed"):
        bus.publish("after-double-dispose")
