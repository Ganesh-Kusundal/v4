"""Contract tests: every broker adapter must satisfy the ``BrokerAdapter`` port.

``test_broker_contracts.py`` locks Dhan↔Upstox *surface symmetry* (each mirrors
the other). This file locks the other direction that matters for a real-money
OMS: **conformance to the port the execution engine depends on**. If an adapter
drops or renames a ``BrokerAdapter`` member, the engine's duck-typed calls would
fail only at runtime in production — this fails at test time instead.

The check is structural (attribute presence), the same thing a
``@runtime_checkable`` ``Protocol`` verifies, but applied to the class so the
credential-bearing Dhan/Upstox adapters need not be instantiated.
"""

from __future__ import annotations

import inspect

import pytest
from tradex_domain.capabilities import BrokerCapabilities
from tradex_domain.protocols import BrokerAdapter

from tradex_brokers.dhan.adapter import DhanBroker
from tradex_brokers.paper.adapter import PaperBroker
from tradex_brokers.upstox.adapter import UpstoxBroker

#: Every concrete adapter the composition root can build. A new adapter must be
#: added here so it is held to the same port.
ADAPTERS = [DhanBroker, UpstoxBroker, PaperBroker]

#: The members a conformant adapter must expose. Methods and properties come
#: from the class body; data members (e.g. ``capabilities``) from annotations.
PORT_MEMBERS = {
    name for name in vars(BrokerAdapter) if not name.startswith("_")
} | set(getattr(BrokerAdapter, "__annotations__", {}))


def test_port_extraction_is_non_trivial() -> None:
    """Guard the guard: a broken extraction must not pass vacuously."""
    assert len(PORT_MEMBERS) >= 20, PORT_MEMBERS
    # Spot-check the members the OMS and market-data paths call directly.
    for critical in (
        "submit_order",
        "cancel_order",
        "modify_order",
        "get_order",
        "get_positions",
        "get_account",
        "history",
        "ltp",
        "get_quote",
        "connect",
        "close",
        "capabilities",
    ):
        assert critical in PORT_MEMBERS, critical


@pytest.mark.parametrize("adapter_cls", ADAPTERS)
def test_adapter_implements_every_port_member(adapter_cls: type) -> None:
    missing = sorted(name for name in PORT_MEMBERS if not hasattr(adapter_cls, name))
    assert not missing, (
        f"{adapter_cls.__name__} does not satisfy the BrokerAdapter port; "
        f"missing: {missing}"
    )


@pytest.mark.parametrize("adapter_cls", ADAPTERS)
def test_capabilities_is_a_property_returning_broker_capabilities(
    adapter_cls: type,
) -> None:
    descriptor = inspect.getattr_static(adapter_cls, "capabilities")
    assert isinstance(descriptor, property), (
        f"{adapter_cls.__name__}.capabilities must be a property"
    )


def test_paper_adapter_satisfies_the_runtime_protocol() -> None:
    """Paper needs no credentials, so it can be checked with ``isinstance``."""
    assert isinstance(PaperBroker(), BrokerAdapter)


def test_paper_capabilities_value_is_a_broker_capabilities() -> None:
    assert isinstance(PaperBroker().capabilities, BrokerCapabilities)
