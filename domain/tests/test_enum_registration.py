"""Tests for open enum registration — register_broker, register_exchange, known_*."""

from __future__ import annotations

import pytest

from tradex_domain.enums import (
    _CUSTOM_BROKER_IDS,
    _CUSTOM_EXCHANGE_IDS,
    BrokerId,
    ExchangeId,
    known_brokers,
    known_exchanges,
    register_broker,
    register_exchange,
)

# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------

@pytest.fixture(autouse=True)
def _clean_custom_registrations():
    """Ensure each test starts with no custom registrations."""
    saved_brokers = dict(_CUSTOM_BROKER_IDS)
    saved_exchanges = dict(_CUSTOM_EXCHANGE_IDS)
    _CUSTOM_BROKER_IDS.clear()
    _CUSTOM_EXCHANGE_IDS.clear()
    yield
    _CUSTOM_BROKER_IDS.clear()
    _CUSTOM_BROKER_IDS.update(saved_brokers)
    _CUSTOM_EXCHANGE_IDS.clear()
    _CUSTOM_EXCHANGE_IDS.update(saved_exchanges)


# ---------------------------------------------------------------------------
# register_broker
# ---------------------------------------------------------------------------

class TestRegisterBroker:
    def test_adds_to_known_brokers(self):
        register_broker("ZERODHA")
        brokers = known_brokers()
        assert "ZERODHA" in brokers

    def test_returns_uppercased_id(self):
        result = register_broker("angel")
        assert result == "ANGEL"

    def test_custom_label(self):
        register_broker("ICICI", label="ICICI Direct")
        assert known_brokers()["ICICI"] == "ICICI Direct"

    def test_default_label_is_broker_id(self):
        register_broker("FYERS")
        assert known_brokers()["FYERS"] == "FYERS"

    def test_duplicate_overwrites(self):
        register_broker("GROW", label="old")
        register_broker("GROW", label="new")
        assert known_brokers()["GROW"] == "new"

    def test_includes_builtin_brokers(self):
        brokers = known_brokers()
        for b in BrokerId:
            assert b.value in brokers


# ---------------------------------------------------------------------------
# register_exchange
# ---------------------------------------------------------------------------

class TestRegisterExchange:
    def test_adds_to_known_exchanges(self):
        register_exchange("SGX")
        assert "SGX" in known_exchanges()

    def test_returns_uppercased_id(self):
        result = register_exchange("cme")
        assert result == "CME"

    def test_custom_label(self):
        register_exchange("LME", label="London Metal Exchange")
        assert known_exchanges()["LME"] == "London Metal Exchange"

    def test_default_label_is_exchange_id(self):
        register_exchange("ADX")
        assert known_exchanges()["ADX"] == "ADX"

    def test_duplicate_overwrites(self):
        register_exchange("TEST_EX", label="v1")
        register_exchange("TEST_EX", label="v2")
        assert known_exchanges()["TEST_EX"] == "v2"

    def test_includes_builtin_exchanges(self):
        exchanges = known_exchanges()
        for e in ExchangeId:
            assert e.value in exchanges


# ---------------------------------------------------------------------------
# known_brokers / known_exchanges — combined
# ---------------------------------------------------------------------------

class TestKnownCombined:
    def test_known_brokers_has_builtins_plus_custom(self):
        register_broker("NEWBROKER")
        brokers = known_brokers()
        # built-in
        assert "DHAN" in brokers
        # custom
        assert "NEWBROKER" in brokers

    def test_known_exchanges_has_builtins_plus_custom(self):
        register_exchange("NEWEX")
        exchanges = known_exchanges()
        # built-in
        assert "NSE" in exchanges
        # custom
        assert "NEWEX" in exchanges
