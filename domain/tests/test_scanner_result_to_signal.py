"""Tests for ScannerResult.to_signal()."""

from __future__ import annotations

from decimal import Decimal

from tradex_domain.enums import AssetClass, ExchangeId, OrderSide
from tradex_domain.instruments import Instrument
from tradex_domain.strategy import ScannerResult
from tradex_domain.value_objects import InstrumentId

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _make_instrument(symbol: str = "RELIANCE") -> Instrument:
    return Instrument(
        instrument_id=InstrumentId(exchange="NSE", underlying=symbol),
        symbol=symbol,
        exchange=ExchangeId.NSE,
        asset_class=AssetClass.EQUITY,
    )


def _make_scanner_result(
    symbol: str = "RELIANCE",
    score: float = 85.5,
    matched_conditions: list[str] | None = None,
    indicator_values: dict[str, float] | None = None,
    rank: int = 1,
    metadata: dict | None = None,
) -> ScannerResult:
    return ScannerResult(
        instrument=_make_instrument(symbol),
        score=score,
        matched_conditions=matched_conditions or ["rsi_oversold", "volume_spike"],
        indicator_values=indicator_values or {"rsi": 28.0, "volume_ratio": 2.5},
        rank=rank,
        metadata=metadata or {},
    )


# ---------------------------------------------------------------------------
# Tests
# ---------------------------------------------------------------------------

class TestToSignalDefaultQuantity:
    def test_default_quantity_is_one(self):
        sr = _make_scanner_result()
        signal = sr.to_signal()
        assert signal.metadata["quantity"] == "1"

    def test_default_returns_buy_signal(self):
        sr = _make_scanner_result()
        signal = sr.to_signal()
        assert signal.direction == OrderSide.BUY


class TestToSignalCustomQuantity:
    def test_custom_quantity(self):
        sr = _make_scanner_result()
        signal = sr.to_signal(quantity=Decimal("100"))
        assert signal.metadata["quantity"] == "100"

    def test_fractional_quantity(self):
        sr = _make_scanner_result()
        signal = sr.to_signal(quantity=Decimal("0.5"))
        assert signal.metadata["quantity"] == "0.5"


class TestToSignalCorrectInstrument:
    def test_instrument_preserved(self):
        sr = _make_scanner_result(symbol="TCS")
        signal = sr.to_signal()
        assert signal.instrument.symbol == "TCS"
        assert signal.instrument.exchange == ExchangeId.NSE

    def test_side_is_buy(self):
        sr = _make_scanner_result()
        signal = sr.to_signal()
        assert signal.is_buy is True
        assert signal.is_sell is False


class TestToSignalMetadata:
    def test_metadata_has_source(self):
        sr = _make_scanner_result()
        signal = sr.to_signal()
        assert signal.metadata["source"] == "scanner"

    def test_metadata_has_score(self):
        sr = _make_scanner_result(score=42.0)
        signal = sr.to_signal()
        assert signal.metadata["score"] == "42.0"

    def test_strength_matches_score(self):
        sr = _make_scanner_result(score=99.9)
        signal = sr.to_signal()
        assert signal.strength == 99.9

    def test_reason_contains_rank_and_conditions(self):
        sr = _make_scanner_result(rank=3, matched_conditions=["macd_cross"])
        signal = sr.to_signal()
        assert "rank=3" in signal.reason
        assert "macd_cross" in signal.reason
