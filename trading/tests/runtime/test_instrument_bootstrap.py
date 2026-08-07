"""Instrument bootstrap tests — domain-level instrument creation.

Ported from v3 ``test_internal_instrument_bootstrap.py``.

v4 API differences:
- Broker-specific loading (Dhan/Upstox) skipped — brokers are stubs
- Port domain-level logic: Equity creation, InstrumentRegistry
"""

from __future__ import annotations

from tradex_domain import Equity
from tradex_domain.wire import InstrumentRegistry

# ---------------------------------------------------------------------------
# Domain-level instrument creation
# ---------------------------------------------------------------------------


class TestInstrumentCreation:
    """Equity and InstrumentRegistry domain contracts."""

    def test_equity_of_creates_canonical_instrument(self) -> None:
        eq = Equity.of("NSE", "RELIANCE")
        assert eq.symbol == "RELIANCE"
        assert eq.exchange.value == "NSE"

    def test_equity_instrument_id_is_stable(self) -> None:
        eq1 = Equity.of("NSE", "RELIANCE")
        eq2 = Equity.of("NSE", "RELIANCE")
        assert eq1.instrument_id == eq2.instrument_id

    def test_different_symbols_have_different_ids(self) -> None:
        eq1 = Equity.of("NSE", "RELIANCE")
        eq2 = Equity.of("NSE", "TCS")
        assert eq1.instrument_id != eq2.instrument_id


# ---------------------------------------------------------------------------
# InstrumentRegistry — register and resolve
# ---------------------------------------------------------------------------


class TestInstrumentRegistry:
    """InstrumentRegistry — provider key mapping."""

    def test_register_and_resolve(self) -> None:
        registry = InstrumentRegistry()
        eq = Equity.of("NSE", "RELIANCE")
        registry.register(eq.instrument_id, {"key": "2885"})
        assert registry.resolve("2885") == eq.instrument_id

    def test_resolve_missing_returns_none(self) -> None:
        registry = InstrumentRegistry()
        assert registry.resolve("nonexistent") is None

    def test_multiple_registrations(self) -> None:
        registry = InstrumentRegistry()
        eq1 = Equity.of("NSE", "RELIANCE")
        eq2 = Equity.of("NSE", "TCS")
        registry.register(eq1.instrument_id, {"key": "2885"})
        registry.register(eq2.instrument_id, {"key": "2900"})
        assert registry.resolve("2885") == eq1.instrument_id
        assert registry.resolve("2900") == eq2.instrument_id
