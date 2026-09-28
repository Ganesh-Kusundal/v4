"""Ported from v3 ``wsd/test_infra.py`` — reconnect config + transport helpers.

The v4 circuit-breaker and retry configuration types were removed with the
resilience stack (fetch-based path carries no resilience); the reconnect and
transport surface that remains is covered here.
"""

from __future__ import annotations

import pytest

from tradex_brokers.common.transport import HttpTransport
from tradex_brokers.common.ws_reconnect import (
    ReconnectConfig,
    WSReconnectManager,
    WsReconnectManager,
)

# ---------------------------------------------------------------------------
# ReconnectConfig — ported from v3
# ---------------------------------------------------------------------------


def test_reconnect_config_defaults() -> None:
    cfg = ReconnectConfig()
    assert cfg.max_retries == 10
    # AC-12/3b: 5s base escalating to a 300s ceiling, retried indefinitely.
    assert cfg.base_delay == 5.0
    assert cfg.max_delay == 300.0
    assert cfg.exponential_base == 2.0
    assert cfg.jitter is True


def test_reconnect_config_frozen() -> None:
    cfg = ReconnectConfig(max_retries=5)
    with pytest.raises(AttributeError):
        cfg.max_retries = 99  # type: ignore[misc]


# ---------------------------------------------------------------------------
# WsReconnectManager alias — ported from v3
# ---------------------------------------------------------------------------


def test_ws_reconnect_manager_alias() -> None:
    assert WsReconnectManager is WSReconnectManager


def test_ws_reconnect_manager_basic() -> None:
    mgr = WsReconnectManager(max_retries=3, base_delay=1.0, jitter=False)
    delay = mgr.next_delay()
    assert delay == 1.0  # base cadence before the escalation point
    assert mgr.attempt_count == 1
    mgr.reset()
    assert mgr.attempt_count == 0


# ---------------------------------------------------------------------------
# Transport convenience methods
# ---------------------------------------------------------------------------


def test_transport_convenience_methods_exist() -> None:
    transport = HttpTransport(base_url="https://api.example.com")
    assert hasattr(transport, "request")
    assert hasattr(transport, "get")
    assert hasattr(transport, "post")
    assert hasattr(transport, "put")
    assert hasattr(transport, "delete")
