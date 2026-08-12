"""Instrument registry consolidation: key encoding, atomic reload.

Complements ``test_registry_key_semantics.py``. Focuses on the consolidated
``InstrumentRegistry`` surface: canonical key encoding for derivatives,
``provider_key``/``meta`` semantics, and the ``WireAdapter`` protocol contract.
"""

from __future__ import annotations

from datetime import date

from tradex_domain.instruments import Future, Option
from tradex_domain.value_objects import InstrumentId
from tradex_domain.wire import (
    InstrumentRegistry,
    WireAdapter,
    normalize_symbol,
)


def _future() -> InstrumentId:
    return Future.of("NFO", "NIFTY", date(2026, 12, 31)).instrument_id


def _option() -> InstrumentId:
    return Option.of("NFO", "NIFTY", date(2026, 12, 31), 20000, "CE").instrument_id


def _equity(symbol: str = "RELIANCE") -> InstrumentId:
    return InstrumentId.equity("NSE", symbol)


class TestKeyEncoding:
    """Canonical key encoding for derivatives (via default registration keys)."""

    def test_future_key_encodes_expiry(self) -> None:
        registry = InstrumentRegistry()
        registry.register(_future(), {})
        assert registry.resolve("NFO_FUT|NIFTY:20261231:FUT") == _future()

    def test_option_key_encodes_expiry_strike_right(self) -> None:
        registry = InstrumentRegistry()
        registry.register(_option(), {})
        assert registry.resolve("NFO_OPT|NIFTY:20261231:20000:CE") == _option()

    def test_equity_key(self) -> None:
        registry = InstrumentRegistry()
        registry.register(_equity(), {})
        assert registry.resolve("NSE_EQ|RELIANCE") == _equity()

    def test_instrument_key_round_trip(self) -> None:
        """The default registration key must reverse-resolve to the instrument."""
        registry = InstrumentRegistry()
        for iid in (_equity(), _future(), _option()):
            registry.register(iid, {})
            assert registry.resolve(registry.provider_key(iid)) == iid


class TestProviderKeyAndMeta:
    def test_provider_key_none_for_unregistered(self) -> None:
        registry = InstrumentRegistry()
        assert registry.provider_key(_equity()) is None

    def test_provider_key_first_registration_wins(self) -> None:
        registry = InstrumentRegistry()
        iid = _equity()
        registry.register(iid, {"key": "first"})
        registry.register(iid, {"key": "second"})
        assert registry.provider_key(iid) == "first"

    def test_meta_returns_copy(self) -> None:
        """meta() must not leak the internal dict for mutation."""
        registry = InstrumentRegistry()
        iid = _equity()
        registry.register(iid, {"key": "NSE:RELIANCE", "lot_size": "100"})
        meta = registry.meta(iid)
        meta["tampered"] = True
        assert "tampered" not in registry.meta(iid)

    def test_meta_merges_incremental_enrichment(self) -> None:
        registry = InstrumentRegistry()
        iid = _equity()
        registry.register(iid, {"key": "NSE:RELIANCE", "asset_class": "EQUITY"})
        registry.register(iid, {"key": "NSE:RELIANCE", "isin": "INE002A01018"})
        meta = registry.meta(iid)
        assert meta["asset_class"] == "EQUITY"
        assert meta["isin"] == "INE002A01018"


class TestNormalization:
    def test_normalize_symbol_strips_suffixes(self) -> None:
        assert normalize_symbol(" reliance-eq ") == "RELIANCE"
        assert normalize_symbol("TCS-BE") == "TCS"
        assert normalize_symbol("NIFTY-FUT") == "NIFTY"

    def test_normalize_symbol_preserves_spaces(self) -> None:
        assert normalize_symbol("NIFTY 50") == "NIFTY 50"


class TestWireAdapterProtocol:
    def test_registry_satisfies_wire_adapter(self) -> None:
        """InstrumentRegistry must be a runtime-checkable WireAdapter."""
        registry = InstrumentRegistry()
        assert isinstance(registry, WireAdapter)

    def test_register_implements_protocol_resolve(self) -> None:
        registry = InstrumentRegistry()
        iid = _equity()
        registry.register(iid, {"key": "NSE:RELIANCE"})
        registry.add_alias("reliance", iid)
        assert registry.resolve("NSE:RELIANCE") == iid
        assert registry.resolve("RELIANCE") == iid
