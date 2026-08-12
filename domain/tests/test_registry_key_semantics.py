"""Registry key semantics: first-registration-wins, aliases, no silent re-point.

The MCX chain path registers bare security ids (``\"560977\"``) after the
master registered the canonical prefixed key (``\"MCX:560977\"``). The primary
provider key must stay the *first* registration; later keys become aliases —
never deleting the canonical key, never clobbering the first metadata.
"""

from __future__ import annotations

from datetime import date

import pytest

from tradex_domain.errors import SDKError
from tradex_domain.instruments import Option
from tradex_domain.value_objects import InstrumentId
from tradex_domain.wire import InstrumentRegistry


def _mcx_option(strike: int = 7150) -> InstrumentId:
    return Option.of("MCX", "CRUDEOIL", date(2026, 8, 17), strike, "CE").instrument_id


def _nse_equity(symbol: str = "RELIANCE") -> InstrumentId:
    return InstrumentId.equity("NSE", symbol)


class TestFirstRegistrationWins:
    def test_primary_key_is_first_registration(self) -> None:
        """Master registers ``MCX:<id>`` first; the chain's bare id later must
        not re-point the primary provider key."""
        registry = InstrumentRegistry()
        iid = _mcx_option()
        registry.register(iid, {"key": "MCX:560977", "asset_class": "OPTION"})
        registry.register(iid, {"key": "560977"})  # chain path

        assert registry.provider_key(iid) == "MCX:560977"

    def test_second_key_stays_resolvable(self) -> None:
        """Both the canonical and the bare key resolve to the same instrument."""
        registry = InstrumentRegistry()
        iid = _mcx_option()
        registry.register(iid, {"key": "MCX:560977", "asset_class": "OPTION"})
        registry.register(iid, {"key": "560977"})

        assert registry.resolve("MCX:560977") == iid
        assert registry.resolve("560977") == iid

    def test_first_metadata_not_clobbered(self) -> None:
        """The chain's ``{\"key\": ...}``-only registration must not wipe the
        master's asset_class metadata (history mapping depends on it)."""
        registry = InstrumentRegistry()
        iid = _mcx_option()
        registry.register(iid, {"key": "MCX:560977", "asset_class": "OPTION"})
        registry.register(iid, {"key": "560977"})

        meta = registry.meta(iid)
        assert meta["asset_class"] == "OPTION"
        assert meta["key"] == "MCX:560977"

    def test_different_instruments_same_key_still_collides(self) -> None:
        """The alias relaxation never allows two distinct instruments to share
        a key — collisions still raise SDKError."""
        registry = InstrumentRegistry()
        a = _nse_equity("RELIANCE")
        b = _nse_equity("TCS")
        registry.register(a, {"key": "NSE:RELIANCE"})
        with pytest.raises(SDKError, match="collision"):
            registry.register(b, {"key": "NSE:RELIANCE"})

    def test_reverse_primary_key(self) -> None:
        registry = InstrumentRegistry()
        iid = _mcx_option()
        registry.register(iid, {"key": "MCX:560977"})
        registry.register(iid, {"key": "560977"})
        assert registry.resolve("MCX:560977") == iid
        assert registry.resolve("560977") == iid


class TestAuthoritativeMasterReload:
    def test_full_master_load_owns_primary_key(self) -> None:
        """A master load (register_authoritative) sets the primary key; a bare
        chain id registered later stays an alias and never shadows it."""
        registry = InstrumentRegistry()
        iid = _mcx_option()
        registry.register_authoritative(iid, "MCX:560977", {"asset_class": "OPTION"})
        registry.register(iid, {"key": "560977"})  # chain endpoint

        assert registry.provider_key(iid) == "MCX:560977"
        assert registry.resolve("560977") == iid

    def test_refresh_repoints_rotated_security_id(self) -> None:
        """Daily master refresh: a re-listed security id in a fresh download
        re-points the primary (previously the first-wins rule would have kept
        the stale key forever on the live ensure_master_fresh path)."""
        registry = InstrumentRegistry()
        iid = _mcx_option()
        registry.register_authoritative(iid, "MCX:560977", {"asset_class": "OPTION"})
        # Fresh download with a rotated id (e.g. corporate action re-listing).
        registry.register_authoritative(iid, "MCX:571200", {"asset_class": "OPTION"})

        assert registry.provider_key(iid) == "MCX:571200"
        assert registry.resolve("MCX:571200") == iid
        assert registry.resolve("MCX:560977") is None  # stale dropped

    def test_refresh_does_not_kill_chain_alias(self) -> None:
        """Re-pointing on refresh keeps the bare-id alias resolvable (Dhan WS
        frames and REST identify by the numeric id alone)."""
        registry = InstrumentRegistry()
        iid = _mcx_option()
        registry.register_authoritative(iid, "MCX:560977", {"asset_class": "OPTION"})
        registry.add_alias("560977", iid)
        registry.register_authoritative(iid, "MCX:571200", {"asset_class": "OPTION"})

        assert registry.resolve("560977") == iid  # alias survives refresh

    def test_incremental_register_merges_metadata(self) -> None:
        """Later registrations enrich metadata instead of dropping it."""
        registry = InstrumentRegistry()
        iid = _mcx_option()
        registry.register(iid, {"key": "MCX:560977", "asset_class": "OPTION"})
        registry.register(iid, {"key": "560977", "lot_size": "100"})

        meta = registry.meta(iid)
        assert meta["asset_class"] == "OPTION"
        assert meta["lot_size"] == "100"
        assert meta["key"] == "MCX:560977"


class TestReplaceAllAtomicReload:
    """``replace_all`` swaps a freshly built registry in atomically (full-master
    reload path): rotated keys re-point, stale keys drop, and registrations
    for instruments the fresh master does not define are carried over — never
    deleted by a reload.
    """

    def test_reload_repoints_rotated_key(self) -> None:
        """A rotated security id in the fresh master becomes the primary and the
        stale key stops resolving (daily-refresh parity)."""
        registry = InstrumentRegistry()
        iid = _mcx_option()
        registry.register_authoritative(iid, "MCX:560977", {"asset_class": "OPTION"})

        fresh = InstrumentRegistry()
        fresh.register_authoritative(iid, "MCX:571200", {"asset_class": "OPTION"})
        fresh.add_alias("571200", iid)
        registry.replace_all(fresh)

        assert registry.provider_key(iid) == "MCX:571200"
        assert registry.resolve("MCX:571200") == iid
        assert registry.resolve("MCX:560977") is None  # stale dropped
        assert registry.resolve("571200") == iid

    def test_reload_keeps_unrelated_registrations(self) -> None:
        """Instruments absent from the fresh master keep their keys/aliases."""
        registry = InstrumentRegistry()
        reloaded = _mcx_option()
        other = _nse_equity("TCS")
        registry.register_authoritative(reloaded, "MCX:560977", {"asset_class": "OPTION"})
        registry.register(other, {"key": "NSE:TCS", "asset_class": "EQUITY"})
        registry.add_alias("TCS", other)

        fresh = InstrumentRegistry()
        fresh.register_authoritative(reloaded, "MCX:571200", {"asset_class": "OPTION"})
        registry.replace_all(fresh)

        assert registry.provider_key(other) == "NSE:TCS"
        assert registry.resolve("TCS") == other
        assert registry.meta(other)["asset_class"] == "EQUITY"

    def test_reload_supersedes_chain_alias_of_master_instrument(self) -> None:
        """A bare chain alias registered before reload is superseded by the
        fresh master's own aliases (the adapter re-adds the current security
        id on every load)."""
        registry = InstrumentRegistry()
        iid = _mcx_option()
        registry.register_authoritative(iid, "MCX:560977", {"asset_class": "OPTION"})
        registry.register(iid, {"key": "560977"})  # chain path alias

        fresh = InstrumentRegistry()
        fresh.register_authoritative(iid, "MCX:571200", {"asset_class": "OPTION"})
        fresh.add_alias("571200", iid)
        registry.replace_all(fresh)

        assert registry.provider_key(iid) == "MCX:571200"
        assert registry.resolve("560977") is None
        assert registry.resolve("571200") == iid

    def test_metadata_meta_survives_reload(self) -> None:
        """Fresh master metadata (asset_class plus contract fields) lands intact."""
        registry = InstrumentRegistry()
        iid = _mcx_option()
        registry.register_authoritative(iid, "MCX:560977", {"asset_class": "OPTION"})

        fresh = InstrumentRegistry()
        fresh.register_authoritative(
            iid,
            "MCX:571200",
            {"asset_class": "OPTION", "instrument_type": "OPTFUT", "lot_size": "250"},
        )
        registry.replace_all(fresh)

        meta = registry.meta(iid)
        assert meta["asset_class"] == "OPTION"
        assert meta["instrument_type"] == "OPTFUT"
        assert meta["lot_size"] == "250"
        assert registry.resolve("MCX:571200") == iid


class TestProviderKeyTagByAssetClass:
    """Residual review Task 2: the provider key tag must follow the
    InstrumentId's asset class, not a hard-coded 'EQ'."""

    def test_index_key_uses_idx_tag(self) -> None:
        registry = InstrumentRegistry()
        iid = InstrumentId.index("NSE", "NIFTY")
        registry.register(iid, {})
        assert registry.resolve("NSE_IDX|NIFTY") == iid

    def test_currency_key_uses_cur_tag(self) -> None:
        registry = InstrumentRegistry()
        iid = InstrumentId.currency("NSE", "USDINR")
        registry.register(iid, {})
        assert registry.resolve("NSE_CUR|USDINR") == iid

    def test_commodity_key_uses_com_tag(self) -> None:
        registry = InstrumentRegistry()
        iid = InstrumentId.commodity("MCX", "GOLD")
        registry.register(iid, {})
        assert registry.resolve("MCX_COM|GOLD") == iid

    def test_equity_key_still_eq(self) -> None:
        registry = InstrumentRegistry()
        iid = InstrumentId.equity("NSE", "TCS")
        registry.register(iid, {})
        assert registry.resolve("NSE_EQ|TCS") == iid
