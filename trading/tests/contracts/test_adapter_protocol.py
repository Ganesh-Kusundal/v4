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
from tradex_domain.capabilities import (
    BrokerCapabilities,
    dhan_capabilities,
    paper_capabilities,
    require_capability,
    upstox_capabilities,
)
from tradex_domain.errors import CapabilityNotSupportedError, SDKError
from tradex_domain.protocols import BrokerAdapter, ExtensionAdapter
from tradex_domain.wire import (
    InstrumentRegistry,
    WireAdapter,
    normalize_exchange,
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
    assert caps.max_batch_size >= 1
    assert AssetClass.EQUITY in caps.supported_asset_classes


def test_dhan_capability_matrix_is_truthful() -> None:
    caps = dhan_capabilities()
    assert caps.supports_market_order is True
    assert caps.supports_limit_order is True
    assert caps.supports_stop_order is True
    assert caps.supports_modify is True
    assert caps.supports_cancel is True
    assert caps.supports_super_order is True
    assert caps.supports_forever_order is True
    assert caps.supports_slice_order is True
    assert caps.supports_edis is True
    assert caps.supports_batch_market_data is True
    assert caps.supports_portfolio_stream is False
    assert caps.supports_option_chain is True
    assert caps.supports_future_chain is True
    assert caps.supports_kill_switch is True
    assert caps.max_batch_size == 1000
    assert caps.depth_levels == 20
    assert caps.max_stream_instruments == 1000


def test_upstox_capability_matrix_is_truthful() -> None:
    caps = upstox_capabilities()
    assert caps.supports_market_order is True
    assert caps.supports_limit_order is True
    assert caps.supports_stop_order is True
    assert caps.supports_modify is True
    assert caps.supports_cancel is True
    assert caps.supports_super_order is False
    assert caps.supports_forever_order is True
    assert caps.supports_slice_order is True
    assert caps.supports_edis is False
    assert caps.supports_batch_market_data is True
    assert caps.supports_portfolio_stream is True
    assert caps.supports_option_chain is True
    assert caps.supports_future_chain is True
    assert caps.supports_kill_switch is True
    assert caps.max_batch_size == 500
    assert caps.depth_levels == 30
    assert caps.max_stream_instruments == 500


def test_paper_capabilities_are_conservative() -> None:
    caps = paper_capabilities()
    assert caps.supports_market_order is True
    assert caps.supports_limit_order is True
    assert caps.supports_modify is True
    assert caps.supports_cancel is True
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
    require_capability(paper_capabilities(), "supports_cancel")


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


class _StubExtensionAdapter(_StubAdapter):
    def submit_super_order(self, request: object) -> object: ...
    def submit_forever_order(self, request: object) -> object: ...
    def submit_slice_order(
        self, request: object, slices: int, interval: object | None
    ) -> list[object]: ...
    def submit_edis(self, request: object) -> object: ...


def test_extension_adapter_protocol() -> None:
    assert isinstance(_StubExtensionAdapter(), ExtensionAdapter)
    assert not isinstance(_StubAdapter(), ExtensionAdapter)


# ---------------------------------------------------------------------------
# WireAdapter + InstrumentRegistry
# ---------------------------------------------------------------------------


def _registry() -> InstrumentRegistry:
    reg = InstrumentRegistry()
    eq = Equity.of("NSE", "RELIANCE")
    reg.register(eq.instrument_id, {"key": "NSE_EQ|RELIANCE", "security_id": "2885"})
    return reg


def test_normalize_symbol_and_exchange() -> None:
    assert normalize_symbol(" reliance-eq ") == "RELIANCE"
    assert normalize_symbol("nifty 50") == "NIFTY 50"
    assert normalize_exchange(" nse ") == "NSE"
    assert normalize_exchange("nfo") == "NFO"


def test_instrument_key_round_trip() -> None:
    reg = _registry()
    eq = Equity.of("NSE", "RELIANCE")
    key = reg.instrument_key(eq.instrument_id)
    assert key == "NSE_EQ|RELIANCE"
    assert reg.reverse_instrument_key(key) == eq.instrument_id


def test_reverse_unknown_key_returns_none() -> None:
    reg = _registry()
    assert reg.reverse_instrument_key("BOGUS|NOPE") is None


def test_register_duplicate_key_collision_rejected() -> None:
    reg = _registry()
    other = Equity.of("NSE", "TCS").instrument_id
    with pytest.raises(SDKError):
        reg.register(other, {"key": "NSE_EQ|RELIANCE"})


def test_register_bulk_rejects_conflicting_rows() -> None:
    reg = InstrumentRegistry()
    rows = [
        {"symbol": "RELIANCE", "exchange": "NSE", "key": "NSE_EQ|RELIANCE"},
        {"symbol": "TCS", "exchange": "NSE", "key": "NSE_EQ|RELIANCE"},
    ]
    with pytest.raises(SDKError):
        reg.register_bulk(rows)


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
