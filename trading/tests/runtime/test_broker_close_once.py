"""M4 — broker.close() called twice in the teardown path.

The principal-architect review (M4) found that ``RuntimeContext.close()``
calls ``self.session.stop()`` (which closes the broker) and then
calls ``self.broker.close()`` again. The fix: drop the second call.
``session.stop()`` owns the broker lifecycle.

Tests pin: in the teardown path (boot → boot_context.close), the
broker's ``close()`` method is called exactly once.
"""

from __future__ import annotations

from unittest.mock import MagicMock

from tradex_trading.config.schema import AppConfig
from tradex_trading.runtime import startup as startup_mod


def test_broker_close_called_exactly_once_per_teardown() -> None:
    """RED: ``RuntimeContext.close()`` must not call ``broker.close()`` twice.

    The pre-fix code calls ``self.broker.close()`` after
    ``self.session.stop()`` (which already closes the broker). The
    double-call is a no-op when ``close()`` is idempotent but a
    latent bug for any broker whose ``close()`` raises on a second
    call. Pin: the broker sees ``close()`` exactly once per
    teardown path.
    """
    cfg = AppConfig(mode="paper")
    rc = startup_mod.boot_context(cfg)
    try:
        broker = rc.broker
        original_close = broker.close
        close_calls: list[None] = []
        def counted_close() -> None:
            close_calls.append(None)
            original_close()
        broker.close = counted_close  # type: ignore[method-assign,assignment]
    finally:
        rc.close()
    assert len(close_calls) == 1, (
        f"broker.close() must be called exactly once per teardown; "
        f"got {len(close_calls)}"
    )
