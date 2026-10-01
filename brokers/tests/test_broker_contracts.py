"""Lockstep guard: the Dhan and Upstox adapters must expose mirrored surfaces.

REF-8 established that both adapters compose the same shared mixins
(``SharedPortfolioMixin`` / ``SharedAdminMixin``) and inherit the same
``BaseBroker`` pass-through wall; every remaining surface difference must be a
deliberate, documented per-broker asymmetry. These tests fail when one adapter
gains (or loses) a public method the other does not mirror, unless the method
is listed in the explicit allowlist below — so the next mirrored method is
forced to land in ``brokers/src/tradex_brokers/common/mixins/`` (or be added
to both) instead of silently drifting.

The allowlist is populated from the REF-8 kept-per-broker decisions: wire
formats differ for these methods, so they intentionally exist on one adapter
only.
"""

from __future__ import annotations

from typing import Any

import pytest

from tradex_brokers.dhan.adapter import DhanBroker
from tradex_brokers.upstox.adapter import UpstoxBroker


def _public_surface(cls: type) -> set[str]:
    """Public (non-underscore) methods and properties across the full MRO."""
    names: set[str] = set()
    for klass in cls.__mro__:
        for name, member in vars(klass).items():
            if name.startswith("_"):
                continue
            if callable(member) or isinstance(member, property):
                names.add(name)
    return names


#: Public methods that exist only on ``DhanBroker`` — each entry documents why
#: the asymmetry is deliberate (REF-8 kept-per-broker decisions).
DHAN_ONLY: dict[str, str] = {
    "get_order_list": (
        "Dhan-only filtered order-list endpoint (GET /orders?status=…); "
        "Upstox has no filtered list surface."
    ),
    "margin_calculator": (
        "Dhan POST /margincalculator (with order_type nuances); Upstox "
        "exposes the analogous surface as margin()."
    ),
    "get_trade_history": (
        "Dhan GET /trades with securityId/date filters; Upstox only has a "
        "day trade book (get_trade_book)."
    ),
    "trade_book": (
        "Dhan name for today's executed trades (GET /trades); Upstox names "
        "the same concept get_trade_book."
    ),
    "mass_status": (
        "Dhan whole-account snapshot composite (orders + positions + "
        "account); no Upstox equivalent."
    ),
    "generate_tpin": (
        "Dhan eDIS surface (CDSL TPIN read via edis_status); Upstox eDIS is "
        "unsupported."
    ),
    "edis_status": "Dhan eDIS status endpoint; Upstox eDIS is unsupported.",
    "authorize_edis": (
        "Dhan eDIS authorization endpoint; Upstox eDIS is unsupported."
    ),
}

#: Public methods that exist only on ``UpstoxBroker`` — each entry documents
#: why the asymmetry is deliberate (REF-8 kept-per-broker decisions).
UPSTOX_ONLY: dict[str, str] = {
    "margin": (
        "Upstox POST /margin/requirement; Dhan exposes the analogous surface "
        "as margin_calculator()."
    ),
    "get_trade_book": (
        "Upstox day trade book (get-trades-for-day); Dhan names the same "
        "concept trade_book."
    ),
    "get_ohlc": (
        "Upstox GET /market-quote/ohlc snapshot (1d/I1/I30); Dhan has no "
        "OHLC-snapshot endpoint."
    ),
    "intraday_candles": (
        "Upstox v3 intraday candle endpoint; Dhan serves current-day candles "
        "through history()."
    ),
    "get_news": (
        "Upstox v2 news feed (supports_news); Dhan has no news endpoint."
    ),
}


@pytest.mark.parametrize("name", sorted(_public_surface(DhanBroker)))
def test_every_dhan_public_method_is_mirrored_on_upstox(name: str) -> None:
    if name in DHAN_ONLY:
        pytest.skip(f"documented Dhan-only asymmetry: {DHAN_ONLY[name]}")
    assert hasattr(UpstoxBroker, name), (
        f"UpstoxBroker lacks DhanBroker public method {name!r}; mirror it via "
        "brokers/src/tradex_brokers/common/mixins/ or add it to DHAN_ONLY "
        "with a reason."
    )


@pytest.mark.parametrize("name", sorted(_public_surface(UpstoxBroker)))
def test_every_upstox_public_method_is_mirrored_on_dhan(name: str) -> None:
    if name in UPSTOX_ONLY:
        pytest.skip(f"documented Upstox-only asymmetry: {UPSTOX_ONLY[name]}")
    assert hasattr(DhanBroker, name), (
        f"DhanBroker lacks UpstoxBroker public method {name!r}; mirror it via "
        "brokers/src/tradex_brokers/common/mixins/ or add it to UPSTOX_ONLY "
        "with a reason."
    )


@pytest.mark.parametrize("name", sorted(DHAN_ONLY))
def test_dhan_allowlist_entries_are_actually_asymmetric(name: str) -> None:
    """Stale-allowlist guard: a listed method must exist only on Dhan."""
    assert hasattr(DhanBroker, name)
    assert not hasattr(UpstoxBroker, name), (
        f"{name!r} is now symmetric — remove it from DHAN_ONLY and, if both "
        "bodies are identical, move it into common/mixins/."
    )


@pytest.mark.parametrize("name", sorted(UPSTOX_ONLY))
def test_upstox_allowlist_entries_are_actually_asymmetric(name: str) -> None:
    """Stale-allowlist guard: a listed method must exist only on Upstox."""
    assert hasattr(UpstoxBroker, name)
    assert not hasattr(DhanBroker, name), (
        f"{name!r} is now symmetric — remove it from UPSTOX_ONLY and, if both "
        "bodies are identical, move it into common/mixins/."
    )


@pytest.mark.parametrize("broker_cls", [DhanBroker, UpstoxBroker])
def test_adapters_compose_the_shared_ref8_mixins(broker_cls: type) -> None:
    """Both adapters must keep the shared mixins on their class bases."""
    mro_names = {klass.__name__ for klass in broker_cls.__mro__}
    assert "SharedPortfolioMixin" in mro_names
    assert "SharedAdminMixin" in mro_names


def _mro_owner(cls: type, name: str) -> Any:
    for klass in cls.__mro__:
        if name in vars(klass):
            return vars(klass)[name]
    raise AssertionError(f"{cls.__name__} has no {name!r}")


def test_shared_mixin_members_resolve_as_expected() -> None:
    """get_portfolio stays owned by the adapter's installed pass-through wall
    (the mixins must not shadow it); invalidate_read_cache is supplied by
    SharedAdminMixin on both adapters."""
    for broker_cls in (DhanBroker, UpstoxBroker):
        owner = next(
            klass
            for klass in broker_cls.__mro__
            if "get_portfolio" in vars(klass)
        )
        assert owner is broker_cls, (
            f"{broker_cls.__name__} pass-through wall must own get_portfolio "
            f"(found owner {owner.__name__})"
        )
    from tradex_brokers.common.mixins import SharedAdminMixin

    assert _mro_owner(DhanBroker, "invalidate_read_cache") is _mro_owner(
        SharedAdminMixin, "invalidate_read_cache"
    )
    assert _mro_owner(UpstoxBroker, "invalidate_read_cache") is _mro_owner(
        SharedAdminMixin, "invalidate_read_cache"
    )
