from gateway_support import FakeBroker, FakeSession, gateway_config
from tradex_gateway.brokers import BrokerRuntime, build_runtimes


def test_build_runtimes_uses_session_factory_and_exposes_session_broker():
    broker = FakeBroker("dhan")
    session = FakeSession(broker)
    runtimes = build_runtimes(gateway_config(brokers=("dhan",)), session_factory=lambda name: session)
    assert runtimes["dhan"].broker is broker
    assert runtimes["dhan"].available


def test_runtime_close_stops_the_session():
    broker = FakeBroker("dhan")
    session = FakeSession(broker)
    runtime = BrokerRuntime("dhan", broker, session=session)
    runtime.close()
    assert session.stopped


def test_failed_session_boot_leaves_broker_unavailable():
    def boom(name):
        raise RuntimeError("boot failed")

    runtimes = build_runtimes(gateway_config(brokers=("dhan",)), session_factory=boom)
    assert runtimes["dhan"].available is False
    assert runtimes["dhan"].error is not None
