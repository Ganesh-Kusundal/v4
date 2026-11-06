from __future__ import annotations

from gateway_support import FakeClock
from tradex_gateway.brokers import BrokerHealth, BrokerRuntime


def test_cooldown_blocks_then_expires():
    clock = FakeClock(100.0)
    health = BrokerHealth(clock)
    health.record_failure("quotes", 30.0, "429")
    assert health.available("quotes") is False
    assert health.retry_after("quotes") == 30.0
    clock.advance(30.0)
    assert health.available("quotes") is True
    health.record_success("quotes")
    snapshot = health.snapshot()["quotes"]
    assert snapshot["available"] is True
    assert snapshot["consecutive_failures"] == 0
    assert snapshot["last_error"] is None


def test_runtime_without_broker_is_unavailable():
    runtime = BrokerRuntime("dhan", None, error="no credentials")
    assert runtime.available is False
    assert runtime.instrument("NSE:RELIANCE") is None
    assert runtime.search("REL") == []
    runtime.close()
