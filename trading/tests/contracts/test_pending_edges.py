"""Pending-edge contracts — ported from v3.

Tests meaningful failure/edge behavior: analytics edge cases (short inputs,
option pricing), wire registry atomicity, and generic serialization
coercion for variadic tuples and nested types.

Skipped from v3 (deferred to later waves or not portable):
- Scanner tests (v4 ScannerEngine is a stub)
- Datalake tests (v4 DataCatalog has different API)
- Infra failure paths (rate_limiter, retry, circuit_breaker, transport,
  token_scheduler, durable_token_manager) — ported in Waves 3/5
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import date
from decimal import Decimal
from enum import Enum
from uuid import UUID

import pytest
from tradex_domain import InstrumentId, Price, SDKError
from tradex_domain.serialization import Serializable
from tradex_domain.wire import InstrumentRegistry

from tradex_trading.analytics.indicators import ema, rsi, sma
from tradex_trading.analytics.options import black_scholes_call, intrinsic_call
from tradex_trading.analytics.reports import max_drawdown, sharpe_ratio

# ---------------------------------------------------------------------------
# Analytics edge cases — indicator short inputs + option pricing
# ---------------------------------------------------------------------------


def test_indicator_short_inputs_and_option_expiry_edges() -> None:
    # v4 indicators return None-padded lists (same length as input) when
    # the input is shorter than the period, unlike v3 which returned [].
    assert sma([], 3) == []
    assert ema([1.0], 2) == [None]
    assert rsi([1.0], 14) == [None]
    # rsi with exactly period+1 values → first RSI appears at index period
    assert rsi([1.0, 1.0, 1.0], 2) == [None, None, 100.0]
    # sharpe_ratio with a single return → 0.0 (need ≥ 2 for std-dev)
    assert sharpe_ratio([100.0]) == 0.0
    assert max_drawdown([]) == 0.0
    # Option pricing edge cases
    assert intrinsic_call(95.0, 100.0) == 0.0
    assert black_scholes_call(105.0, 100.0, 0.0, 0.05, 0.2) == 5.0
    assert black_scholes_call(105.0, 100.0, 1.0, 0.05, 0.0) == 5.0


# ---------------------------------------------------------------------------
# Wire registry atomicity
# ---------------------------------------------------------------------------


def test_wire_bulk_registration_is_atomic_on_collision() -> None:
    registry = InstrumentRegistry()
    registry.register(
        InstrumentId.equity("NSE", "BASE"),
        {"key": "NSE_EQ|BASE"},
    )

    with pytest.raises(SDKError):
        registry.register_bulk(
            [
                {"symbol": "NEW", "exchange": "NSE", "key": "NSE_EQ|NEW"},
                {"symbol": "OTHER", "exchange": "NSE", "key": "NSE_EQ|BASE"},
            ]
        )

    assert registry.reverse_instrument_key("NSE_EQ|NEW") is None
    assert registry.reverse_instrument_key("NSE_EQ|BASE") == InstrumentId.equity("NSE", "BASE")


def test_wire_bulk_replacement_removes_stale_provider_key() -> None:
    registry = InstrumentRegistry()
    instrument = InstrumentId.equity("NSE", "RELIANCE")
    registry.register_bulk([{"symbol": "RELIANCE", "exchange": "NSE", "key": "old-key"}])

    registry.register_bulk([{"symbol": "RELIANCE", "exchange": "NSE", "key": "new-key"}])

    assert registry.reverse_instrument_key("old-key") is None
    assert registry.reverse_instrument_key("new-key") == instrument
    assert registry.provider_key(instrument) == "new-key"


# ---------------------------------------------------------------------------
# Serialization — variadic tuple + nested types
# ---------------------------------------------------------------------------


class _Color(Enum):
    RED = "red"


@dataclass(frozen=True, slots=True)
class _SerializableEdge(Serializable):
    prices: tuple[Price, ...]
    color: _Color
    metadata: dict[str, int]
    uid: UUID
    day: date


def test_serialization_handles_variadic_tuple_and_nested_types() -> None:
    value = _SerializableEdge(
        prices=(Price(value=Decimal("1")), Price(value=Decimal("2"))),
        color=_Color.RED,
        metadata={"count": 2},
        uid=UUID("12345678-1234-5678-1234-567812345678"),
        day=date(2026, 8, 1),
    )

    assert _SerializableEdge.from_dict(value.to_dict()) == value
