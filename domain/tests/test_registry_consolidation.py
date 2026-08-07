"""Instrument registry consolidation: bulk loads, key encoding, atomic reload.

Complements ``test_registry_key_semantics.py``. Focuses on the consolidated
``InstrumentRegistry`` surface: ``register_bulk`` (atomic authoritative
replacement + collision rejection), canonical ``instrument_key`` encoding for
derivatives, ``provider_key``/``meta`` semantics, and the ``WireAdapter``
protocol contract.
"""

from __future__ import annotations

from datetime import date

import pytest

from tradex_domain.errors import SDKError
from tradex_domain.instruments import Future, Option
from tradex_domain.value_objects import InstrumentId
from tradex_domain.wire import (
    InstrumentRegistry,
    WireAdapter,
    normalize_exchange,
    normalize_symbol,
)


def _future() -> InstrumentId:
    return Future.of("NFO", "NIFTY", date(2026, 12, 31)).instrument_id


def _option() -> InstrumentId:
    return Option.of("NFO", "NIFTY", date(2026, 12, 31), 20000, "CE").instrument_id


def _equity(symbol: str = "RELIANCE") -> InstrumentId:
    return InstrumentId.equity("NSE", symbol)


class TestBulkRegistration:
    """``register_bulk`` — atomic authoritative replacement for master loads."""

    def test_register_bulk_creates_entries(self) -> None:
        registry = InstrumentRegistry()
        rows = [
            {
                "symbol": "RELIANCE",
                "exchange": "NSE",
                "key": "NSE:RELIANCE",
                "asset_class": "EQUITY",
            },
            {"symbol": "TCS", "exchange": "NSE", "key": "NSE:TCS", "asset_class": "EQUITY"},
        ]
        registry.register_bulk(rows)
        assert registry.resolve("NSE:RELIANCE") == _equity("RELIANCE")
        assert registry.resolve("NSE:TCS") == _equity("TCS")
        assert registry.provider_key(_equity("RELIANCE")) == "NSE:RELIANCE"
        assert registry.provider_key(_equity("TCS")) == "NSE:TCS"

    def test_register_bulk_is_authoritative_replacement(self) -> None:
        """A bulk load re-points the primary and drops stale keys (master refresh)."""
        registry = InstrumentRegistry()
        iid = _equity("RELIANCE")
        registry.register(iid, {"key": "old-key", "asset_class": "EQUITY"})
        assert registry.reverse_instrument_key("old-key") == iid

        registry.register_bulk(
            [{"symbol": "RELIANCE", "exchange": "NSE", "key": "new-key", "asset_class": "EQUITY"}]
        )
        assert registry.provider_key(iid) == "new-key"
        assert registry.reverse_instrument_key("new-key") == iid
        assert registry.reverse_instrument_key("old-key") is None

    def test_register_bulk_uses_default_key_when_absent(self) -> None:
        registry = InstrumentRegistry()
        registry.register_bulk([{"symbol": "RELIANCE", "exchange": "NSE"}])
        iid = _equity("RELIANCE")
        assert registry.resolve("NSE_EQ|RELIANCE") == iid

    def test_register_bulk_rejects_collision(self) -> None:
        """Two distinct symbols mapping to the same key must raise SDKError."""
        registry = InstrumentRegistry()
        rows = [
            {"symbol": "RELIANCE", "exchange": "NSE", "key": "shared"},
            {"symbol": "TCS", "exchange": "NSE", "key": "shared"},
        ]
        with pytest.raises(SDKError, match="collision"):
            registry.register_bulk(rows)

    def test_register_bulk_is_atomic_on_collision(self) -> None:
        """A collision must not leave earlier rows partially applied."""
        registry = InstrumentRegistry()
        rows = [
            {"symbol": "RELIANCE", "exchange": "NSE", "key": "RELIANCE"},
            {"symbol": "TCS", "exchange": "NSE", "key": "RELIANCE"},  # collides with row 0
        ]
        with pytest.raises(SDKError, match="collision"):
            registry.register_bulk(rows)
        # Nothing from the failed batch should have been applied.
        assert registry.resolve("RELIANCE") is None
        assert registry.resolve("TCS") is None


class TestKeyEncoding:
    """Canonical ``instrument_key`` encoding for derivatives."""

    def test_future_key_encodes_expiry(self) -> None:
        key = InstrumentRegistry.instrument_key(_future())
        assert key == "NFO_FUT|NIFTY:20261231:FUT"

    def test_option_key_encodes_expiry_strike_right(self) -> None:
        key = InstrumentRegistry.instrument_key(_option())
        assert key == "NFO_OPT|NIFTY:20261231:20000:CE"

    def test_equity_key(self) -> None:
        key = InstrumentRegistry.instrument_key(_equity())
        assert key == "NSE_EQ|RELIANCE"

    def test_instrument_key_round_trip(self) -> None:
        """The generated key must reverse-resolve back to the same instrument."""
        registry = InstrumentRegistry()
        for iid in (_equity(), _future(), _option()):
            key = registry.instrument_key(iid)
            registry.register(
                iid,
                {"key": key, "asset_class": "FUTURE" if iid.right == "FUT" else "OPTION"},
            )
            assert registry.reverse_instrument_key(key) == iid


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

    def test_normalize_exchange(self) -> None:
        assert normalize_exchange(" nse ") == "NSE"
        assert normalize_exchange("nfo") == "NFO"


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
