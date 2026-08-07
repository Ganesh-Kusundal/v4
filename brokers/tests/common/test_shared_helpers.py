"""Shared response/value helpers in ``common/provider_common`` are the single
source of truth (DRY). Broker clients alias these locally (``_as_price``,
``_unwrap_data``, ``_first_mapping``); this test guards against behavioral
drift if a client re-introduces a divergent local copy.
"""

from __future__ import annotations

from collections.abc import Mapping

from tradex_brokers.common.provider_common import (
    as_price,
    first_mapping,
    unwrap_data,
)


def test_as_price_none_and_empty_are_zero() -> None:
    assert as_price(None) == as_price("0") == as_price("")
    assert as_price(None).value == 0


def test_as_price_parses_decimal_string() -> None:
    from decimal import Decimal

    assert as_price("123.45").value == Decimal("123.45")


def test_unwrap_data_strips_envelope() -> None:
    assert unwrap_data({"data": 1}) == 1
    assert unwrap_data({"other": 2}) == {"other": 2}
    assert unwrap_data(7) == 7


def test_first_mapping_prefers_mapping() -> None:
    assert first_mapping({"a": 1}) == {"a": 1}
    assert first_mapping([{"b": 2}, {"c": 3}]) == {"b": 2}
    assert dict(first_mapping([])) == {}


def test_local_aliases_match_common() -> None:
    """Every broker aliases the shared helpers; verify parity with source."""
    import tradex_brokers.dhan.client as dhan_client
    import tradex_brokers.upstox.client as upstox_client

    for mod in (dhan_client, upstox_client):
        assert mod._as_price is as_price
        assert mod._unwrap_data is unwrap_data
        assert mod._first_mapping is first_mapping
        # Behavior parity for a representative value.
        assert mod._as_price("9.5") == as_price("9.5")
        assert mod._unwrap_data({"data": {"x": 1}}) == {"x": 1}
        assert isinstance(mod._first_mapping({"k": 1}), Mapping)


def test_helper_imports_available_in_client_modules() -> None:
    # Importing the clients must not fail after dedup (regression guard).
    import tradex_brokers.dhan.client  # noqa: F401
    import tradex_brokers.upstox.client  # noqa: F401
