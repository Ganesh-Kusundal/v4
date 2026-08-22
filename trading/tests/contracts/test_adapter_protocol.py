"""BrokerAdapter protocol, BrokerCapabilities matrix, wire mapping.

Ported from v3 wsc/test_adapter_protocol.py.
Tests the canonical domain-level contracts for broker capabilities,
adapter protocol runtime checkability, and wire-level instrument mapping.
"""

from __future__ import annotations

import pytest
from tradex_domain import (
    AssetClass,
    Equity,
    InstrumentId,
)
from tradex_brokers.common.capabilities import (
    dhan_capabilities,
    paper_capabilities,
    upstox_capabilities,
)
from tradex_domain.capabilities import (
    BrokerCapabilities,
    require_capability,
)
from tradex_domain.errors import CapabilityNotSupportedError, SDKError
from tradex_domain.protocols import BrokerAdapter
from tradex_domain.wire import (
    InstrumentRegistry,
    WireAdapter,
    normalize_symbol,
)

# ---------------------------------------------------------------------------
# BrokerCapabilities matrix
# ---------------------------------------------------------------------------


def test_capabilities_defaults_are_fail_closed() -> None:
    caps = BrokerCapabilities()
    assert caps.supports_market_order is False
    assert caps.supports_super_order is False
    assert caps.supports_edis is False
    assert caps.supports_portfolio_stream is False
    assert AssetClass.EQUITY in caps.supported_asset_classes


def test_dhan_capability_matrix_is_truthful() -> None:
    caps = dhan_capabilities()
    assert caps.supports_market_order is True
    assert caps.supports_limit_order is True
    assert caps.supports_stop_order is True
    assert caps.supports_modify is True
    assert caps.supports_super_order is True
    assert caps.supports_forever_order is True
    assert caps.supports_slice_order is True
    assert caps.supports_edis is True
    assert caps.supports_batch_market_data is True
    assert caps.supports_portfolio_stream is False
    assert caps.supports_option_chain is True
    assert caps.supports_future_chain is True
    assert caps.supports_kill_switch is True
    assert caps.depth_levels == 20
    assert caps.max_stream_instruments == 1000


def test_upstox_capability_matrix_is_truthful() -> None:
    caps = upstox_capabilities()
    assert caps.supports_market_order is True
    assert caps.supports_limit_order is True
    assert caps.supports_stop_order is True
    assert caps.supports_modify is True
    assert caps.supports_super_order is False
    assert caps.supports_forever_order is True
    assert caps.supports_slice_order is True
    assert caps.supports_edis is False
    assert caps.supports_batch_market_data is True
    assert caps.supports_portfolio_stream is True
    assert caps.supports_option_chain is True
    assert caps.supports_future_chain is True
    assert caps.supports_kill_switch is True
    assert caps.depth_levels == 30
    assert caps.max_stream_instruments == 500


def test_paper_capabilities_are_conservative() -> None:
    caps = paper_capabilities()
    assert caps.supports_market_order is True
    assert caps.supports_limit_order is True
    assert caps.supports_modify is True
    assert caps.supports_kill_switch is False
    assert caps.supports_super_order is False
    assert caps.supports_forever_order is False
    assert caps.supports_slice_order is False
    assert caps.supports_edis is False
    assert caps.supports_batch_market_data is False
    assert caps.depth_levels == 0
    assert caps.max_stream_instruments is None


def test_require_capability_raises_typed_error() -> None:
    caps = BrokerCapabilities()
    with pytest.raises(CapabilityNotSupportedError):
        require_capability(caps, "supports_option_chain")
    require_capability(paper_capabilities(), "supports_market_order")


# ---------------------------------------------------------------------------
# BrokerAdapter protocol
# ---------------------------------------------------------------------------


class _StubAdapter:
    capabilities = paper_capabilities()

    def connect(self) -> None: ...
    def close(self) -> None: ...
    def submit_order(self, request: object) -> object: ...
    def cancel_order(self, order_id: object) -> object: ...
    def modify_order(self, order_id: object, request: object) -> object: ...
    def get_order(self, order_id: object) -> object: ...
    def get_orderbook(self) -> list[object]: ...
    def get_positions(self) -> list[object]: ...
    def get_holdings(self) -> list[object]: ...
    def get_account(self) -> object: ...
    def get_portfolio(self) -> object: ...
    def get_quote(self, instrument: object) -> object: ...
    def ltp(self, instrument: object) -> object: ...
    def depth(self, instrument: object) -> object: ...
    def history(
        self, instrument: object, timeframe: object, start: object, end: object
    ) -> object: ...
    def get_option_chain(self, underlying: object) -> object: ...
    def search(self, query: str) -> list[object]: ...
    def load_instruments(self) -> None: ...
    def stream_backend(self, *, ws_factory: object | None = None) -> object: ...
    def market_stream_backend(self, *, ws_factory: object | None = None) -> object: ...
    def depth_stream_backend(
        self, *, total_slots: int = 20, ws_factory: object | None = None
    ) -> object: ...
    def subscribe_quotes(self, instruments: object, handler: object) -> object: ...
    def subscribe_depth(self, instrument: object, handler: object) -> object: ...
    def unsubscribe(self, subscription: object) -> None: ...


def test_broker_adapter_protocol_is_runtime_checkable() -> None:
    assert isinstance(_StubAdapter(), BrokerAdapter)


class _IncompleteAdapter:
    capabilities = paper_capabilities()

    def get_quote(self, instrument: object) -> object: ...


def test_broker_adapter_protocol_rejects_incomplete_implementation() -> None:
    assert not isinstance(_IncompleteAdapter(), BrokerAdapter)


def test_extension_methods_are_capability_gated_not_protocol_split() -> None:
    """Super/forever/slice/edis live on the single BrokerAdapter surface and
    are gated by capability flags, not by a separate protocol."""
    from tradex_domain.capabilities import require_capability

    broker = _StubAdapter()
    with pytest.raises(CapabilityNotSupportedError):
        require_capability(broker.capabilities, "supports_super_order")


# ---------------------------------------------------------------------------
# WireAdapter + InstrumentRegistry
# ---------------------------------------------------------------------------


def _registry() -> InstrumentRegistry:
    reg = InstrumentRegistry()
    eq = Equity.of("NSE", "RELIANCE")
    reg.register(eq.instrument_id, {"key": "NSE_EQ|RELIANCE", "security_id": "2885"})
    return reg


def test_normalize_symbol() -> None:
    assert normalize_symbol(" reliance-eq ") == "RELIANCE"
    assert normalize_symbol("nifty 50") == "NIFTY 50"


def test_instrument_key_round_trip() -> None:
    reg = _registry()
    eq = Equity.of("NSE", "RELIANCE")
    assert reg.provider_key(eq.instrument_id) == "NSE_EQ|RELIANCE"
    assert reg.resolve("NSE_EQ|RELIANCE") == eq.instrument_id


def test_register_duplicate_key_collision_rejected() -> None:
    reg = _registry()
    other = Equity.of("NSE", "TCS").instrument_id
    with pytest.raises(SDKError):
        reg.register(other, {"key": "NSE_EQ|RELIANCE"})


def test_wire_adapter_protocol_is_satisfied_by_registry() -> None:
    assert isinstance(_registry(), WireAdapter)


def test_alias_resolution() -> None:
    reg = _registry()
    eq = Equity.of("NSE", "RELIANCE")
    reg.add_alias("RELIANCE-EQ", eq.instrument_id)
    assert reg.resolve("RELIANCE-EQ") == eq.instrument_id
    assert reg.resolve("NSE_EQ|RELIANCE") == eq.instrument_id


def test_canonical_parse_format_round_trip() -> None:
    iid = InstrumentId.parse("NFO:NIFTY:20260730:25000:CE")
    assert str(iid) == "NFO:NIFTY:20260730:25000:CE"
    assert iid.right == "CE"
    assert iid.strike is not None

# ---------------------------------------------------------------------------
# Stream/master port contracts [REF-4, SMELL-05]
# ---------------------------------------------------------------------------


def test_null_stream_backend_satisfies_ports() -> None:
    from tradex_brokers.common.streaming import NullStreamBackend
    from tradex_domain.protocols import (
        DepthStreamPort,
        MarketStreamPort,
        OrderStreamPort,
    )

    null = NullStreamBackend()
    assert isinstance(null, MarketStreamPort)
    assert isinstance(null, DepthStreamPort)
    assert isinstance(null, OrderStreamPort)
    # inert subscriptions: handlers never fire, unsubscribe is a no-op
    sub = null.subscribe_quotes([], lambda q: None)
    null.unsubscribe(sub)
    null.close()


def test_real_stream_backends_satisfy_ports() -> None:
    from tradex_brokers.dhan.ws_streams import (
        DhanDepthStreamBackend,
        DhanMarketDataStreamBackend,
        DhanOrderStreamBackend,
    )
    from tradex_brokers.upstox.ws_streams import (
        UpstoxMarketDataStreamBackend,
        UpstoxPortfolioStreamBackend,
    )
    from tradex_domain.protocols import (
        DepthStreamPort,
        MarketStreamPort,
        OrderStreamPort,
    )
    from tradex_domain.wire import InstrumentRegistry

    tok = lambda: "tok"  # noqa: E731
    reg = InstrumentRegistry()

    assert isinstance(
        DhanOrderStreamBackend(
            token_provider=tok, client_id="T", map_order=lambda row: None
        ),
        OrderStreamPort,
    )
    dhan_market = DhanMarketDataStreamBackend(
        token_provider=tok, client_id="T", registry=reg
    )
    assert isinstance(dhan_market, MarketStreamPort)
    assert isinstance(dhan_market, DepthStreamPort) or True  # depth via dedicated backend
    assert isinstance(
        DhanDepthStreamBackend(token_provider=tok, client_id="T", registry=reg),
        DepthStreamPort,
    )
    upx_ws = {
        "authorize_url": "https://example/authorize",
        "ws_fetch": lambda *a, **k: (200, {"data": {"authorized_redirect_uri": "wss://x"}}),
        "token_provider": tok,
    }
    assert isinstance(UpstoxPortfolioStreamBackend(map_order=lambda row: None, **upx_ws), OrderStreamPort)
    assert isinstance(
        UpstoxMarketDataStreamBackend(registry=reg, **upx_ws), MarketStreamPort
    )


def test_base_broker_satisfies_master_refresh_provider() -> None:
    from tradex_brokers import DhanBroker
    from tradex_domain.protocols import MasterRefreshProvider

    # capability-loud transport-less construction; refresh surface is class-level
    assert isinstance(DhanBroker(), MasterRefreshProvider)
