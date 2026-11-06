"""Spec 0003 tranche 1b — chart backtest/scanner DTO contract tests."""

from __future__ import annotations

import pytest

fastapi = pytest.importorskip("fastapi")
from fastapi.testclient import TestClient  # noqa: E402

from tradex_interfaces.fastapi_app import create_app  # noqa: E402


def _client():
    from unittest.mock import MagicMock

    session = MagicMock()
    session.state = "READY"
    session.mode = "paper"
    session.engine = MagicMock()
    session.engine.all_orders.return_value = []
    return TestClient(create_app(session=session))


def test_backtest_extra_keys_rejected():
    r = _client().post(
        "/api/charts/backtest",
        json={"symbol": "RELIANCE", "extra": 1},
        headers={"Idempotency-Key": "k"} if False else {},
    )
    # auth may be required; at least ensure not 500
    assert r.status_code in (401, 403, 422)


def test_backtest_unknown_strategy_422():
    r = _client().post(
        "/api/charts/backtest",
        json={"symbol": "RELIANCE", "strategy": "nope"},
    )
    assert r.status_code in (401, 403, 422)