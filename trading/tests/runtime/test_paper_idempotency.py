"""Paper idempotency: must use SQLiteIdempotencyGuard.

Paper mode must use SQLiteIdempotencyGuard so duplicate order submissions
are rejected across process restarts, even when no explicit persistence
path is configured.
"""

from __future__ import annotations

from tradex_config.schema import AppConfig
from tradex_domain import BrokerId
from tradex_runtime.startup import boot
from trading.tests.risk_support import permissive_risk


def test_paper_mode_uses_sqlite_idempotency_guard() -> None:
    cfg = AppConfig(
        broker_id=BrokerId.PAPER,
        mode="paper",
        live_enabled=False,
        risk=permissive_risk(),
    )
    session = boot(cfg, wire_strategies=False)
    try:
        guard = session._engine._guard
        assert guard.__class__.__name__ == "SQLiteIdempotencyGuard"
    finally:
        session.stop()
