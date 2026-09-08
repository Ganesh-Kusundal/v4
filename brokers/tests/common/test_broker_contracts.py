"""Capability-matrix contract tests for the broker adapters.

Locks in the "capability-loud" guarantee: for every ``BrokerCapabilities`` flag
a broker leaves ``False``, the corresponding adapter method must raise
``CapabilityNotSupportedError`` (or be absent). For flags set ``True`` the
method is expected to exist and be reachable (the adapter owns the specifics;
no network is touched here). Also asserts registry conformance and the common
``BaseBroker`` lifecycle gates (order-ops disabled -> ``OrderRejectedError``,
not connected -> ``BrokerUnavailableError``).
"""

from __future__ import annotations

from collections.abc import Callable
from decimal import Decimal
from typing import Any

import pytest
from tradex_domain.capabilities import BrokerCapabilities
from tradex_domain.enums import OrderSide, OrderType, TimeInForce
from tradex_domain.errors import (
    AuthenticationError,
    BrokerUnavailableError,
    CapabilityNotSupportedError,
    OrderRejectedError,
)
from tradex_domain.execution import OrderRequest
from tradex_domain.instruments import Equity
from tradex_domain.protocols import BrokerAdapter
from tradex_domain.value_objects import OrderId, Quantity

from tradex_brokers.dhan.adapter import DhanBroker
from tradex_brokers.paper.adapter import PaperBroker
from tradex_brokers.upstox.adapter import UpstoxBroker

_INSTRUMENT = Equity.of("NSE", "RELIANCE")

ALL_BROKERS = [
    pytest.param(PaperBroker, id="paper"),
    pytest.param(DhanBroker, id="dhan"),
    pytest.param(UpstoxBroker, id="upstox"),
]


def _order_request() -> OrderRequest:
    return OrderRequest(
        instrument=_INSTRUMENT,
        side=OrderSide.BUY,
        order_type=OrderType.MARKET,
        quantity=Quantity(value=Decimal(1)),
        time_in_force=TimeInForce.DAY,
    )


#: capability flag -> methods that must raise CapabilityNotSupportedError when
#: the flag is False. Each entry is a ``(method_name, args_factory)`` pair.
#: ``args_factory`` is called fresh per broker to avoid shared mutable state.
CAPABILITY_METHODS: dict[str, list[tuple[str, Callable[[], tuple[Any, ...]]]]] = {
    "supports_super_order": [
        ("submit_super_order", lambda: (_order_request(),)),
        ("modify_super_order", lambda: (OrderId("1"), _order_request())),
        ("cancel_super_order", lambda: (OrderId("1"),)),
        ("list_super_orders", lambda: ()),
    ],
    "supports_forever_order": [
        ("submit_forever_order", lambda: (_order_request(),)),
        ("modify_forever_order", lambda: (OrderId("1"), _order_request())),
        ("cancel_forever_order", lambda: (OrderId("1"),)),
        ("list_forever_orders", lambda: ()),
    ],
    "supports_slice_order": [
        ("submit_slice_order", lambda: (_order_request(), 2, None)),
    ],
    "supports_edis": [
        ("submit_edis", lambda: (_order_request(),)),
        ("generate_tpin", lambda: ()),
        ("edis_status", lambda: ("INE123A",)),
        ("authorize_edis", lambda: ("INE123A", 1, "NSE")),
    ],
    "supports_kill_switch": [
        ("kill_switch", lambda: (True,)),
        ("status_kill_switch", lambda: ()),
    ],
    "supports_option_chain": [
        ("get_option_chain", lambda: (_INSTRUMENT, None)),
    ],
    "supports_future_chain": [
        ("future_chain", lambda: (_INSTRUMENT,)),
    ],
    "supports_batch_market_data": [
        ("ltp_batch", lambda: ([_INSTRUMENT],)),
        ("quote_batch", lambda: ([_INSTRUMENT],)),
    ],
    "supports_news": [
        ("get_news", lambda: ()),
    ],
}


@pytest.mark.parametrize("broker_cls", ALL_BROKERS)
def test_conforms_to_protocol(broker_cls: type) -> None:
    instance = broker_cls()
    assert isinstance(instance, BrokerAdapter)


@pytest.mark.parametrize("broker_cls", ALL_BROKERS)
def test_capabilities_is_frozen_matrix(broker_cls: type) -> None:
    caps = broker_cls().capabilities
    assert isinstance(caps, BrokerCapabilities)
    # frozen dataclass — assignment must fail
    with pytest.raises(Exception):
        caps.supports_market_order = True  # type: ignore[misc]


@pytest.mark.parametrize("broker_cls", ALL_BROKERS)
def test_unsupported_capabilities_are_capability_loud(broker_cls: type) -> None:
    broker = broker_cls()
    broker.connect()
    caps = broker.capabilities
    for flag, methods in CAPABILITY_METHODS.items():
        if getattr(caps, flag):
            # Supported — adapter owns specifics; only require the method exists.
            for name, _ in methods:
                assert hasattr(broker, name), (
                    f"{broker_cls.__name__} claims {flag} but lacks {name}"
                )
            continue
        for name, args_factory in methods:
            if not hasattr(broker, name):
                continue  # absent method = unsupported; acceptable
            method = getattr(broker, name)
            with pytest.raises(CapabilityNotSupportedError):
                method(*args_factory())


@pytest.mark.parametrize("broker_cls", ALL_BROKERS)
def test_order_operations_gate(broker_cls: type) -> None:
    if broker_cls is PaperBroker:
        pytest.skip("paper broker is standalone and has no order-ops gate")
    broker = broker_cls()
    broker.connect()
    broker.set_order_operations_enabled(False)
    with pytest.raises(OrderRejectedError):
        broker.submit_order(_order_request())
    with pytest.raises(OrderRejectedError):
        broker.cancel_order(OrderId("1"))
    with pytest.raises(OrderRejectedError):
        broker.modify_order(OrderId("1"), _order_request())


@pytest.mark.parametrize("broker_cls", ALL_BROKERS)
def test_unconnected_raises_broker_unavailable(broker_cls: type) -> None:
    if broker_cls is PaperBroker:
        pytest.skip("paper broker connects on construction")
    broker = broker_cls()  # never connect()
    with pytest.raises(BrokerUnavailableError):
        broker.get_order(OrderId("1"))
    with pytest.raises(BrokerUnavailableError):
        broker.get_account()


def test_paper_streaming_is_safe_noop() -> None:
    """Paper declares no streaming capability; its no-ops must not raise."""
    broker = PaperBroker()
    assert broker.subscribe_quotes([_INSTRUMENT], lambda q: None) is None
    assert broker.subscribe_depth(_INSTRUMENT, lambda d: None) is None
    assert broker.stream_backend() is None
    assert broker.market_stream_backend() is None
    assert broker.depth_stream_backend() is None


def test_base_broker_connect_without_transport_is_capability_loud() -> None:
    broker = DhanBroker()
    broker.connect()  # no transport: logically connected, but _require() fails
    with pytest.raises(BrokerUnavailableError):
        broker.submit_order(_order_request())


def test_close_is_idempotent() -> None:
    broker = DhanBroker()
    broker.connect()
    broker.close()
    broker.close()  # second close must not raise
    with pytest.raises(BrokerUnavailableError):
        broker.get_order(OrderId("1"))


def test_verify_connection_probes_account() -> None:
    class _AccountTransport:
        def __init__(self, ok: bool) -> None:
            self._ok = ok
            self.called = 0

        def get_account(self) -> object:
            self.called += 1
            if not self._ok:
                raise RuntimeError("boom")
            return object()

    ok_broker = DhanBroker(transport=_AccountTransport(True))
    ok_broker.connect()
    assert ok_broker.verify_connection() is True

    bad_broker = DhanBroker(transport=_AccountTransport(False))
    bad_broker.connect()
    assert bad_broker.verify_connection() is False


def test_verify_connection_probes_when_expired_token_can_be_minted() -> None:
    """An expired cached token is not a failure for mint-capable managers.

    Regression: ``verify_connection`` used to short-circuit to False whenever
    the token manager reported its cached token expired — even though the
    manager can mint a fresh token on demand and every real request would
    authenticate. The probe must run (and pass) after the on-demand mint.
    """

    class _MintOnDemand:
        def __init__(self) -> None:
            self.ensure_calls = 0

        def is_expired(self) -> bool:
            return True

        def ensure_token(self, **kwargs: Any) -> str:
            self.ensure_calls += 1
            return "fresh-token"

    class _AccountTransport:
        def __init__(self) -> None:
            self.called = 0

        def get_account(self) -> object:
            self.called += 1
            return object()

    manager = _MintOnDemand()
    transport = _AccountTransport()
    broker = DhanBroker(transport=transport)
    broker.set_token_manager(manager)  # type: ignore[arg-type]
    broker.connect()

    assert broker.verify_connection() is True
    assert manager.ensure_calls == 1  # fresh token obtained on demand
    assert transport.called == 1  # wire probe ran — no false negative


def test_verify_connection_short_circuits_when_expired_token_cannot_refresh() -> None:
    """Expired token with no way to obtain a replacement skips the probe.

    The token manager already knows the token is dead and nothing can mint a
    new one, so the wire probe is not worth the provider quota — and would
    fail for the same reason.
    """

    class _StaleOnly:
        def is_expired(self) -> bool:
            return True
        # No ensure_token: no mint/refresh strategy exists.

    class _AccountTransport:
        def __init__(self) -> None:
            self.called = 0

        def get_account(self) -> object:
            self.called += 1
            return object()

    transport = _AccountTransport()
    broker = DhanBroker(transport=transport)
    broker.set_token_manager(_StaleOnly())  # type: ignore[arg-type]
    broker.connect()

    assert broker.verify_connection() is False
    assert transport.called == 0  # probe was not spent


def test_verify_connection_refuses_when_on_demand_mint_fails() -> None:
    """A failed on-demand mint reports False without burning the probe."""

    class _FailingMint:
        def is_expired(self) -> bool:
            return True

        def ensure_token(self, **kwargs: Any) -> str:
            raise AuthenticationError("mint rejected: Invalid TOTP")

    class _AccountTransport:
        def __init__(self) -> None:
            self.called = 0

        def get_account(self) -> object:
            self.called += 1
            return object()

    transport = _AccountTransport()
    broker = DhanBroker(transport=transport)
    broker.set_token_manager(_FailingMint())  # type: ignore[arg-type]
    broker.connect()

    assert broker.verify_connection() is False
    assert transport.called == 0



def test_connect_runs_instrument_loader_once() -> None:
    rows = [
        {
            "symbol": "RELIANCE",
            "exchange": "NSE",
            "security_id": "2885",
            "asset_class": "EQUITY",
        }
    ]
    calls: list[int] = []

    def loader() -> list[dict[str, Any]]:
        calls.append(1)
        return rows

    broker = DhanBroker(transport=object(), instrument_loader=loader)
    broker.connect()
    broker.connect()
    assert len(calls) == 1
    assert broker._loaded_instruments
    assert broker.search("RELIANCE")
