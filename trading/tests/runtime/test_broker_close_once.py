"""M4 — broker.close() is called exactly once, by TradingSession.stop()."""

from __future__ import annotations

from tradex_config.schema import AppConfig
from tradex_runtime import startup as startup_mod
from trading.tests.risk_support import permissive_risk


def test_session_stop_closes_the_broker_exactly_once() -> None:
    """``session.stop()`` owns the broker lifecycle; nothing closes it twice."""
    cfg = AppConfig(risk=permissive_risk(), mode="paper")
    session = startup_mod.boot(cfg, wire_strategies=False)
    broker = session.broker
    original_close = broker.close
    close_calls: list[None] = []

    def counted_close() -> None:
        close_calls.append(None)
        original_close()

    broker.close = counted_close  # type: ignore[method-assign]
    session.stop()
    assert len(close_calls) == 1, (
        f"broker.close() must be called exactly once per teardown; got {len(close_calls)}"
    )
