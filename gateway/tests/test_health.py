from __future__ import annotations

import pytest
from gateway_support import FakeClock
from tradex_gateway.brokers import BrokerHealth, BrokerRuntime
from tradex_gateway.errors import NoHealthyBroker


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


def test_health_snapshot_hides_exception_text():
    """A broker failure records the error class only, never message text."""
    from gateway_support import FakeBroker, FakeClock, gateway_config
    from tradex_domain.errors import RateLimitError
    from tradex_gateway.router import MarketDataRouter

    clock = FakeClock()
    dhan = FakeBroker("dhan")
    upstox = FakeBroker("upstox")
    runtimes = {
        "dhan": BrokerRuntime("dhan", dhan, clock=clock),
        "upstox": BrokerRuntime("upstox", upstox, clock=clock),
    }
    router = MarketDataRouter(gateway_config(), runtimes, clock=clock)

    def call(broker):
        raise RateLimitError("https://api.example.com/secret?key=abc rate limited")

    with pytest.raises(NoHealthyBroker):
        router.call("quotes", call)
    snapshot = runtimes["dhan"].health.snapshot()
    assert "api.example.com" not in str(snapshot)
    assert "secret" not in str(snapshot)
    assert snapshot["quotes"]["last_error"] == "RateLimitError"
