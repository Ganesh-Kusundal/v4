from __future__ import annotations

import pytest
from gateway_support import FakeBroker, FakeClock, gateway_config
from tradex_domain.errors import (
    AuthenticationError,
    BrokerUnavailableError,
    ConnectionTimeoutError,
    InstrumentNotFoundError,
    RateLimitError,
    SDKError,
)
from tradex_gateway.brokers import BrokerRuntime
from tradex_gateway.errors import BrokerUnavailable, NoHealthyBroker
from tradex_gateway.router import MarketDataRouter


def _router(
    clock: FakeClock, *brokers: FakeBroker, primary_history: str = "upstox"
) -> MarketDataRouter:
    runtimes = {broker.name: BrokerRuntime(broker.name, broker, clock=clock) for broker in brokers}
    return MarketDataRouter(gateway_config(primary_history=primary_history), runtimes, clock=clock)


def test_primary_failure_fails_over_to_secondary():
    clock = FakeClock()
    dhan, upstox = FakeBroker("dhan"), FakeBroker("upstox")

    def call(broker):
        if broker is dhan:
            raise RateLimitError("429")
        return "secondary"

    router = _router(clock, dhan, upstox, primary_history="dhan")
    result, name = router.call("history", call)
    assert result == "secondary"
    assert name == "upstox"
    # The primary is cooling down for the configured window.
    assert router.call("history", lambda _: "secondary")[1] == "upstox"


def test_all_brokers_failing_raises_no_healthy_broker():
    clock = FakeClock()
    dhan, upstox = FakeBroker("dhan"), FakeBroker("upstox")

    def call(_):
        raise RateLimitError("429")

    router = _router(clock, dhan, upstox)
    with pytest.raises(NoHealthyBroker) as excinfo:
        router.call("history", call)
    assert excinfo.value.retry_after is not None


def test_instrument_not_found_does_not_cool_down():
    clock = FakeClock()
    dhan, upstox = FakeBroker("dhan"), FakeBroker("upstox")

    calls = {"count": 0}

    def call(_):
        calls["count"] += 1
        if calls["count"] == 1:
            raise InstrumentNotFoundError("unknown")
        return "ok"

    runtimes = {
        "dhan": BrokerRuntime("dhan", dhan, clock=clock),
        "upstox": BrokerRuntime("upstox", upstox, clock=clock),
    }
    router = MarketDataRouter(gateway_config(primary_history="dhan"), runtimes, clock=clock)
    result, name = router.call("history", call)
    assert result == "ok"
    assert name == "upstox"
    assert runtimes["dhan"].health.snapshot()["history"]["available"] is True


def test_broker_unavailable_maps_to_gateway_error():
    clock = FakeClock()
    dhan, upstox = FakeBroker("dhan"), FakeBroker("upstox")

    def call(_):
        raise RuntimeError("boom")

    router = _router(clock, dhan, upstox)
    with pytest.raises(BrokerUnavailable):
        router.call("history", call)


@pytest.mark.parametrize(
    ("failure", "cooldown"),
    [
        (RateLimitError("429"), 30.0),
        (ConnectionTimeoutError("timeout"), 15.0),
        (BrokerUnavailableError("down"), 15.0),
        (AuthenticationError("denied"), 60.0),
        (SDKError("sdk"), 5.0),
        (RuntimeError("unexpected"), 5.0),
    ],
)
def test_failure_policy_cools_the_failed_broker_for_the_tabled_window(
    failure: Exception, cooldown: float
):
    clock = FakeClock()
    dhan, upstox = FakeBroker("dhan"), FakeBroker("upstox")
    runtimes = {
        "dhan": BrokerRuntime("dhan", dhan, clock=clock),
        "upstox": BrokerRuntime("upstox", upstox, clock=clock),
    }
    router = MarketDataRouter(gateway_config(primary_history="dhan"), runtimes, clock=clock)

    def call(broker):
        if broker is dhan:
            raise failure
        return "ok"

    assert router.call("history", call) == ("ok", "upstox")
    snapshot = runtimes["dhan"].health.snapshot()["history"]
    assert snapshot["available"] is False
    assert snapshot["cooldown_seconds"] == cooldown
