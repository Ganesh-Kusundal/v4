"""Dhan instrument master CSV loader.

Loads the Dhan instrument master file and normalizes rows into domain
Instrument objects. Supports both the main scrip master and the MCX
supplement CSV.
"""

from __future__ import annotations

from collections.abc import Iterable, Mapping
from datetime import date
from decimal import Decimal
from typing import Any

from tradex_domain.instruments import Future, Instrument, Option
from tradex_domain.wire import InstrumentRegistry, normalize_symbol

# (SEM_EXM_EXCH_ID, SEM_SEGMENT) -> canonical v4 exchange.
# Single source of truth for Dhan master-row exchange normalization —
# consumed by ``tradex_brokers.dhan.master`` and nowhere else in trading.
SEGMENT_CANONICAL: dict[tuple[str, str], str] = {
    ("NSE", "E"): "NSE",
    ("NSE", "D"): "NFO",
    ("NSE", "I"): "IDX",
    ("NSE", "C"): "CDS",
    ("NSE", "M"): "NSE_COMM",
    ("BSE", "E"): "BSE",
    ("BSE", "D"): "BFO",
    ("BSE", "I"): "IDX",
    ("BSE", "C"): "BCD",
    ("MCX", "M"): "MCX",
    ("IDX", "I"): "IDX",
    ("CDS", "D"): "CDS",
}

_FUTURES = {"FUTIDX", "FUTSTK", "FUTCOM", "FUTCUR"}
_OPTIONS = {"OPTIDX", "OPTSTK", "OPTFUT", "OPTCUR"}
_RIGHT_MAP = {"CE": "CE", "PE": "PE", "CA": "CE", "PA": "PE"}


def _value(row: Mapping[str, Any], *names: str) -> str:
    """Extract the first non-empty value from a row by trying multiple field names."""
    for name in names:
        value = row.get(name)
        if value is not None and str(value).strip():
            return str(value).strip()
    return ""


def _parse_expiry(value: str) -> date:
    """Parse expiry date from various formats (YYYY-MM-DD, YYYYMMDD, DD-MM-YYYY)."""
    text = value.strip()
    if " " in text:
        text = text.split(" ", 1)[0]
    if len(text) == 8 and text.isdigit():
        return date(int(text[:4]), int(text[4:6]), int(text[6:8]))
    if len(text) == 10 and text[4] == "-":
        return date.fromisoformat(text)
    parts = text.split("-")
    if len(parts) == 3 and len(parts[2]) == 4:
        return date(int(parts[2]), int(parts[1]), int(parts[0]))
    raise ValueError(f"invalid instrument expiry: {value!r}")


def load_mcx_rows(
    rows: Iterable[Mapping[str, Any]],
    registry: InstrumentRegistry,
) -> list[Instrument]:
    """Load the MCX supplement CSV (different schema from the main scrip master).

    The MCX supplement uses columns like ``Symbol``, ``Expiry``,
    ``StrikePrice``, ``OptionType``, ``SecurityId``, and ``LotSize``.
    """
    instruments: list[Instrument] = []
    for row in rows:
        symbol = normalize_symbol(
            _value(row, "Symbol", "symbol", "TradingSymbol", "trading_symbol")
        )
        expiry_text = _value(row, "Expiry", "expiry", "ExpiryDate")
        if not symbol or not expiry_text:
            continue
        strike_text = _value(row, "StrikePrice", "Strike", "strike")
        right = _RIGHT_MAP.get(
            _value(row, "OptionType", "option_type", "right").upper(), ""
        )
        lot_text = _value(row, "LotSize", "lot_size", "lot")
        sec_id = _value(row, "SecurityId", "security_id", "securityId")
        root = normalize_symbol(
            _value(row, "Underlying", "underlying", "UnderlyingSymbol")
            or symbol.split("-", 1)[0]
        )
        try:
            expiry_date = _parse_expiry(expiry_text)
        except ValueError:
            continue
        if right in {"CE", "PE"} and strike_text:
            try:
                strike = Decimal(strike_text)
            except (TypeError, ValueError):
                continue
            instrument: Instrument = Option.of("MCX", root, expiry_date, strike, right)
            asset_class = "OPTION"
        elif expiry_text and not right:
            instrument = Future.of("MCX", root, expiry_date)
            asset_class = "FUTURE"
        else:
            continue
        meta: dict[str, object] = {"asset_class": asset_class, "raw": dict(row)}
        if lot_text:
            meta["lot_size"] = lot_text
        if sec_id:
            meta["key"] = f"MCX:{sec_id}"
        registry.register(instrument.instrument_id, meta)
        if sec_id:
            registry.add_alias(sec_id, instrument.instrument_id)
        registry.add_alias(symbol, instrument.instrument_id)
        instruments.append(instrument)
    return instruments


__all__ = ["SEGMENT_CANONICAL", "load_mcx_rows"]
