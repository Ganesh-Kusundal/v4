"""Capability-flag anchoring — implementation-anchored capability contracts.

The plain capability-matrix tests assert tables against hardcoded literals
(tautologies). These tests anchor each flag to the ADAPTER IMPLEMENTATION in
both directions:

Forward:  flag is True  ⇒ the adapter actually exposes the corresponding
          surface (methods/endpoints exist).
Reverse:  flag is False ⇒ the gated entry point fails loudly with
          ``CapabilityNotSupportedError`` *before* any transport requirement,
          and adapter-specific surfaces only exist when their flag is True.

A capability table drifting from the implementation now fails CI instead of
silently lying.
"""

from __future__ import annotations

import pytest
from tradex_domain.capabilities import BrokerCapabilities
from tradex_domain.errors import CapabilityNotSupportedError
from tradex_domain.instruments import Equity

from tradex_brokers.common.capabilities import (
    dhan_capabilities,
    paper_capabilities,
    upstox_capabilities,
)
from tradex_brokers.dhan.adapter import DhanBroker
from tradex_brokers.paper.adapter import PaperBroker
from tradex_brokers.upstox.adapter import UpstoxBroker

# ---------------------------------------------------------------------------
# Forward anchors: True flag ⇒ required adapter surface
# ---------------------------------------------------------------------------

_FLAG_SURFACE: dict[type, dict[str, tuple[str, ...]]] = {
    DhanBroker: {
        "supports_market_order": ("submit_order",),
        "supports_limit_order": ("submit_order",),
        "supports_stop_order": ("submit_order",),
        "supports_modify": ("modify_order",),
        "supports_super_order": ("submit_super_order", "list_super_orders"),
        "supports_forever_order": ("submit_forever_order", "list_forever_orders"),
        "supports_slice_order": ("submit_slice_order",),
        "supports_edis": ("generate_tpin", "edis_status", "authorize_edis"),
        "supports_batch_market_data": ("ltp_batch", "quote_batch"),
        "supports_option_chain": ("get_option_chain",),
        "supports_future_chain": ("future_chain",),
        "supports_kill_switch": ("kill_switch", "status_kill_switch"),
    },
    UpstoxBroker: {
        "supports_market_order": ("submit_order",),
        "supports_limit_order": ("submit_order",),
        "supports_stop_order": ("submit_order",),
        "supports_modify": ("modify_order",),
        "supports_forever_order": ("submit_forever_order", "list_forever_orders"),
        "supports_slice_order": ("submit_slice_order",),
        "supports_batch_market_data": ("ltp_batch", "quote_batch"),
        "supports_option_chain": ("get_option_chain",),
        "supports_future_chain": ("future_chain",),
        "supports_kill_switch": ("kill_switch", "status_kill_switch"),
        "supports_news": ("get_news",),
        "supports_portfolio_stream": ("stream_backend",),
    },
    PaperBroker: {
        "supports_market_order": ("submit_order",),
        "supports_limit_order": ("submit_order",),
        "supports_stop_order": ("submit_order",),
        "supports_modify": ("modify_order",),
    },
}


@pytest.mark.parametrize("broker_cls", [DhanBroker, UpstoxBroker, PaperBroker])
def test_every_true_flag_has_its_surface(broker_cls: type) -> None:
    caps = broker_cls().capabilities
    anchors = _FLAG_SURFACE[broker_cls]
    for name, surface in anchors.items():
        if getattr(caps, name):
            for attr in surface:
                assert hasattr(broker_cls, attr), (
                    f"{broker_cls.__name__} claims {name}=True "
                    f"but does not implement {attr!r}"
                )


def test_adapter_defined_surfaces_imply_true_flags() -> None:
    """A provider-specific endpoint implemented in the adapter itself must be
    backed by a True flag — otherwise the surface is dead code or the table
    is lying."""
    # Dhan implements eDIS endpoints natively.
    assert "generate_tpin" in DhanBroker.__dict__
    assert dhan_capabilities().supports_edis is True
    # Upstox implements news natively.
    assert "get_news" in UpstoxBroker.__dict__
    assert upstox_capabilities().supports_news is True


# ---------------------------------------------------------------------------
# Reverse anchors: False flag ⇒ loud typed error before any transport need
# ---------------------------------------------------------------------------

# Gated entry points callable on an unconnected adapter; the capability gate
# fires BEFORE the connected-transport requirement. (method, zero-arg caller)
_GATED_CALLS = {
    "supports_super_order": ("list_super_orders", lambda b: b.list_super_orders()),
    "supports_forever_order": ("list_forever_orders", lambda b: b.list_forever_orders()),
    "supports_kill_switch": ("status_kill_switch", lambda b: b.status_kill_switch()),
    "supports_future_chain": (
        "future_chain",
        lambda b: b.future_chain(Equity.of("NSE", "RELIANCE")),
    ),
}


@pytest.mark.parametrize(
    "broker_cls, caps",
    [
        (DhanBroker, dhan_capabilities()),
        (UpstoxBroker, upstox_capabilities()),
        (PaperBroker, paper_capabilities()),
    ],
    ids=["dhan", "upstox", "paper"],
)
def test_every_false_flag_fails_loud(broker_cls: type, caps: BrokerCapabilities) -> None:
    broker = broker_cls()
    for flag, (method, call) in _GATED_CALLS.items():
        if getattr(caps, flag):
            continue
        if hasattr(broker_cls, method):
            # Surface inherited from the shared base — the gate must fire.
            with pytest.raises(CapabilityNotSupportedError):
                call(broker)
        else:
            # Parallel implementation without the shared wall — the only
            # stronger fail-closure: the surface simply does not exist.
            assert not hasattr(broker, method)


def test_paper_extension_surface_is_structurally_absent() -> None:
    """Paper does not inherit the BaseBroker pass-through wall, so every
    unsupported extension is absent entirely — it cannot even be called."""
    broker = PaperBroker()
    broker.connect()
    for method in (
        "list_super_orders",
        "submit_super_order",
        "list_forever_orders",
        "submit_forever_order",
        "status_kill_switch",
        "kill_switch",
        "submit_slice_order",
        "submit_edis",
        "future_chain",
        "ltp_batch",
    ):
        assert not hasattr(broker, method), (
            f"PaperBroker unexpectedly exposes {method!r} while its "
            f"capability table claims the feature is unsupported"
        )


def test_dhan_upstox_depth_levels_match_declared_backends() -> None:
    """depth_levels > 0 must correspond to a real depth stream backend."""
    assert dhan_capabilities().depth_levels == 20
    assert hasattr(DhanBroker, "depth_stream_backend")
    assert upstox_capabilities().depth_levels == 30
    assert hasattr(UpstoxBroker, "depth_stream_backend")
    # Paper declares no depth feed at all.
    assert paper_capabilities().depth_levels == 0
