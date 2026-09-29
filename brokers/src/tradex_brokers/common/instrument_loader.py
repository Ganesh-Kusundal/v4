"""Unified instrument-master download, parse, and build pipeline.

Single home for what ``tradex_brokers.dhan.master`` and
``tradex_brokers.upstox.master`` each used to own separately: the master
download callables, wire-format row normalization, the segment →
canonical-exchange maps, the MCX supplement loader, and :func:`load_instruments`
— the one entry point that turns a broker's instrument master into domain
``Instrument`` objects.

The one thing the parsers read beyond identity is each security's lifecycle
state (``SEM_STATUS`` / ``status`` → ``master_status``), because AC-12's
per-symbol cadence exemption needs a real halted/delisted source rather than a
value a caller guessed. The vocabulary is ``tradex_domain.instrument_lifecycle``'s,
so both vendors spell the same canonical state and ``tradex_runtime`` can read
back exactly what these parsers wrote.

Broker adapters re-export the pieces their historical import paths expose
(``parse_dhan_master``, ``dhan_master_download``, ``SEGMENT_CANONICAL``, …) so
existing consumers keep working; no trading-layer code parses a broker wire
format itself. Registration into a broker's registry stays with
``BaseBroker.load_instruments``, which consumes the normalized rows.
"""

from __future__ import annotations

import csv
import gzip
import io
import json
from collections.abc import Callable, Iterable, Mapping
from datetime import UTC, date, datetime
from decimal import Decimal
from typing import Any

from tradex_domain import SDKError
from tradex_domain.instrument_lifecycle import row_status
from tradex_domain.instruments import Future, Instrument, Option
from tradex_domain.value_objects import InstrumentId
from tradex_domain.wire import InstrumentRegistry, normalize_symbol

from tradex_brokers.common.instruments import build_instrument_from_row
from tradex_brokers.common.transport import Fetch

DHAN_INSTRUMENT_MASTER_URL = "https://images.dhan.co/api-data/api-scrip-master.csv"
UPSTOX_INSTRUMENT_MASTER_URL = (
    "https://assets.upstox.com/market-quote/instruments/exchange/complete.json.gz"
)

# Real masters carry 100k+ rows (Dhan ~220k, Upstox ~150k). A strict-mode
# download below this floor is a corrupt/truncated fetch — never trust it as the
# instrument master (v2 MIN_*_INSTRUMENTS parity). Injected-fetch paths (tests)
# run non-strict and keep small fixtures working.
MIN_MASTER_ROWS = 10_000

# (SEM_EXM_EXCH_ID, SEM_SEGMENT) -> canonical v4 exchange.
# Single source of truth for Dhan master-row exchange normalization —
# consumed by ``parse_dhan_master`` and nowhere else in trading.
DHAN_SEGMENT_CANONICAL: dict[tuple[str, str], str] = {
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

# Segment -> canonical v4 exchange. Single source of truth for Upstox
# master-row exchange normalization (consumed by ``parse_upstox_master``).
# Includes the BSE/NCDEX/COM variants the trading layer previously re-declared.
UPSTOX_SEGMENT_CANONICAL: dict[str, str] = {
    "NSE_EQ": "NSE",
    "BSE_EQ": "BSE",
    "NSE_FO": "NFO",
    "BSE_FO": "BFO",
    "MCX_FO": "MCX",
    "NSE_COM": "NSE_COMM",
    "BCD_FO": "BCD",
    "NCD_FO": "CDS",
    "NSE_CURRENCY": "CDS",
    "BSE_CURRENCY": "BCD",
    "NSE_INDEX": "IDX",
    "BSE_INDEX": "IDX",
}

_RIGHT_MAP = {"CE": "CE", "PE": "PE", "CA": "CE", "PA": "PE"}


def dhan_master_download(fetch: Fetch, *, strict: bool) -> Callable[[], object]:
    """Return a download callable for the Dhan instrument master CSV."""

    def download() -> object:
        try:
            status, body = fetch("GET", DHAN_INSTRUMENT_MASTER_URL, raw_response=True)
        except Exception:
            if strict:
                raise
            return b""
        if status < 200 or status >= 300:
            if strict:
                raise SDKError(f"Dhan instrument master request failed: HTTP {status}")
            return b""
        return body

    return download


def parse_dhan_master(raw: object, *, strict: bool) -> list[dict[str, Any]]:
    """Parse raw Dhan master bytes/CSV into normalized instrument rows."""
    if isinstance(raw, bytes):
        if not raw:
            return []
        text = raw.decode("utf-8-sig")
    elif isinstance(raw, str):
        text = raw
    elif isinstance(raw, list):
        return [dict(row) for row in raw if isinstance(row, dict)]
    elif isinstance(raw, dict) and isinstance(raw.get("rows"), list):
        return [dict(row) for row in raw["rows"] if isinstance(row, dict)]
    else:
        if strict:
            raise SDKError("Dhan instrument master response was not CSV data")
        return []
    rows = [dict(row) for row in csv.DictReader(io.StringIO(text))]
    if strict and len(rows) < MIN_MASTER_ROWS:
        raise SDKError(
            f"Dhan instrument master has too few rows "
            f"({len(rows)} < {MIN_MASTER_ROWS}); refusing it as the master"
        )
    # Normalize CSV column names into the keys expected by DhanBroker.load_instruments:
    #   symbol, exchange, key, asset_class
    normalized: list[dict[str, Any]] = []
    for row in rows:
        exchange = str(row.get("SEM_EXM_EXCH_ID", "NSE")).strip().upper()
        segment = str(row.get("SEM_SEGMENT", "")).strip().upper()
        # Exchange series (EQ/BE/… and the debt/warrant codes D1/W1/N1). The
        # trading symbol alone does not identify a security: the master lists
        # ``CHOLAFIN`` twice — the equity (series ``EQ``, id 685) and a
        # debenture of the same issuer (series ``D1``, id 19257).
        series = str(row.get("SEM_SERIES") or "").strip().upper()
        symbol = str(row.get("SEM_TRADING_SYMBOL", "")).strip()
        security_id = str(row.get("SEM_SMST_SECURITY_ID", "")).strip()
        instrument_name = str(row.get("SEM_INSTRUMENT_NAME", "")).strip()
        # Canonical exchange from (exchange, segment)
        canonical = DHAN_SEGMENT_CANONICAL.get((exchange, segment), exchange)
        key = f"{canonical}:{security_id}" if security_id else f"{canonical}:{symbol}"
        # Derivatives structure: futures/options rows carry expiry/strike/right
        # plus an underlying root. Keep them so ``load_instruments`` can build
        # typed ``Future``/``Option`` instruments — MCX chains come from here.
        right = str(row.get("SEM_OPTION_TYPE", "")).strip().upper() or None
        expiry_text = str(row.get("SEM_EXPIRY_DATE", "")).strip()
        strike_text = str(row.get("SEM_STRIKE_PRICE", "")).strip()
        # Underlying root (v3 parity): SM_SYMBOL_NAME is the clean root; when it
        # is missing, the trading symbol's first hyphen token is the machine
        # base. SEM_CUSTOM_SYMBOL is a *human display name* (e.g. "EURINR 27
        # MAR 104.75 CALL") that must never become the root — the 204k-row
        # audit found ~87k currency-option rows whose identity broke because
        # the display name was used as the underlying.
        underlying_text = str(row.get("SM_SYMBOL_NAME") or "").strip()
        if not underlying_text and "-" in symbol:
            underlying_text = symbol.split("-", 1)[0]
        if not underlying_text:
            underlying_text = str(row.get("SEM_CUSTOM_SYMBOL") or "").strip()
        # Accurate asset_class. A segment-based label alone would mark every
        # derivative segment (D/C/M) as ``OTHER``; registry meta must carry
        # the real class because ``DhanApiClient._history_instrument_type``
        # and key-tag selection key off it (v3 parity, verified by audit over
        # the real 204k-row master: 0 identity diffs, 180k mislabeled OTHER).
        if segment == "I":
            asset_class = "INDEX"
        elif segment == "E":
            asset_class = "EQUITY"
        elif right in ("CE", "PE"):
            asset_class = "OPTION"
        elif instrument_name.startswith("FUT") or right == "FUT":
            asset_class = "FUTURE"
        else:
            asset_class = "OTHER"
        # Contract metadata the quant layer needs (position sizing, tick
        # maths, slippage): pass through lot/tick from the master columns.
        lot_size = str(row.get("SEM_LOT_UNITS", "") or "").strip() or None
        tick_size = str(row.get("SEM_TICK_SIZE", "") or "").strip() or None
        # The security's lifecycle state (SEM_STATUS). AC-12's cadence
        # exemption needs a real halted/delisted source, and the scrip master is
        # the only place Dhan publishes one. Normalized by
        # ``tradex_domain.instrument_lifecycle`` so both vendors agree on
        # spelling, and ``None`` when the column is absent or says nothing we
        # recognise — "unknown" must never be mistaken for "exempt".
        status = row_status(row)
        normalized.append({
            "symbol": symbol or instrument_name,
            "exchange": canonical,
            "key": key,
            "asset_class": asset_class,
            "security_id": security_id,
            "segment": segment,
            "series": series,
            "instrument_type": instrument_name,
            "right": right,
            # Dhan expiry is a DASHED_DATETIME value ("%Y-%m-%d %H:%M:%S",
            # see tradex_domain.datetime_formats) — the same family the
            # intraday chart fromDate uses. Keep only the date part ISO. This
            # is a split on the separator, not a strptime: the column is
            # already validated as a date by the master, and a lossy parse
            # here would drop rows instead of leaving the text alone.
            "expiry": expiry_text.split(" ", 1)[0] if expiry_text else None,
            "strike": strike_text or None,
            "underlying": underlying_text or None,
            "lot_size": lot_size,
            "tick_size": tick_size,
            "master_status": status,
        })
    return normalized


def upstox_master_download(fetch: Fetch, *, strict: bool) -> Callable[[], object]:
    """Return a download callable for the Upstox instrument master gzip JSON."""

    def download() -> object:
        try:
            status, body = fetch("GET", UPSTOX_INSTRUMENT_MASTER_URL, raw_response=True)
        except Exception:
            if strict:
                raise
            return b""
        if status < 200 or status >= 300:
            if strict:
                raise SDKError(f"Upstox instrument master request failed: HTTP {status}")
            return b""
        return body

    return download


def parse_upstox_master(raw: object, *, strict: bool) -> list[dict[str, Any]]:
    """Parse raw Upstox master bytes/gzip-JSON into normalized instrument rows."""
    if isinstance(raw, bytes):
        if not raw:
            return []
        try:
            decoded = gzip.decompress(raw)
        except OSError:
            decoded = raw
        parsed: object = json.loads(decoded.decode("utf-8"))
    elif isinstance(raw, str):
        parsed = json.loads(raw)
    elif isinstance(raw, list):
        parsed = raw
    elif isinstance(raw, dict) and isinstance(raw.get("rows"), list):
        parsed = raw["rows"]
    else:
        if strict:
            raise SDKError("Upstox instrument master response was not JSON data")
        return []
    if not isinstance(parsed, list):
        raise SDKError("Upstox instrument master must be a JSON array")
    rows = [dict(row) for row in parsed if isinstance(row, dict)]
    # Normalize Upstox exchange names — the master includes non-v4 exchanges
    # like GLOBAL, NCDEX, NSE_COM, etc. that InstrumentId rejects. Only rows
    # whose exchange is a valid v4 exchange survive, so load_instruments can
    # never raise on a row it cannot represent.
    _VALID_EXCHANGES = InstrumentId.VALID_EXCHANGES
    normalized: list[dict[str, Any]] = []
    for row in rows:
        exchange_raw = str(row.get("exchange", "NSE")).strip().upper()
        if exchange_raw not in _VALID_EXCHANGES:
            continue
        segment = str(row.get("segment", "")).strip().upper()
        exchange = UPSTOX_SEGMENT_CANONICAL.get(segment, exchange_raw)
        symbol = str(row.get("trading_symbol", "")).strip().upper()
        if not symbol:
            continue
        # Derivatives structure — Upstox masters carry instrument_type
        # (FUT/CE/PE/FUTIDX/...), ms-epoch expiry, strike and underlying root.
        instrument_type = str(row.get("instrument_type", "")).strip().upper()
        right = instrument_type if instrument_type in ("CE", "PE") else None
        expiry_iso: str | None = None
        expiry_raw = row.get("expiry")
        if isinstance(expiry_raw, (int, float)) and expiry_raw:
            epoch = expiry_raw / 1000 if expiry_raw > 1e12 else expiry_raw
            expiry_iso = datetime.fromtimestamp(epoch, tz=UTC).date().isoformat()
        elif isinstance(expiry_raw, str) and expiry_raw.strip():
            expiry_iso = expiry_raw.strip()[:10]
        strike_raw = row.get("strike_price")
        underlying = str(row.get("underlying_symbol") or row.get("name") or "").strip()
        # Accurate asset_class. The segment is the source of truth for
        # index/equity rows; derivatives get their real class from the
        # instrument type / right so registry meta stays correct (v3 parity).
        if segment in ("NSE_INDEX", "BSE_INDEX"):
            asset_class = "INDEX"
        elif "EQ" in segment:
            asset_class = "EQUITY"
        elif right in ("CE", "PE"):
            asset_class = "OPTION"
        elif instrument_type.startswith("FUT") or instrument_type in {
            "FUT",
            "FUTURE",
            "FUTIDX",
            "FUTSTK",
            "FUTCOM",
            "FUTCUR",
        }:
            asset_class = "FUTURE"
        else:
            asset_class = "OTHER"
        lot_size = str(row.get("lot_size") or "").strip() or None
        # Security lifecycle state (Upstox publishes it as ``status``). Carried
        # through as the canonical ``master_status`` — ``None`` when absent or
        # unrecognised, because an unknown status must never read as an
        # exemption. See tradex_domain.instrument_lifecycle.
        status = row_status(row)
        # Map Upstox fields to the normalized keys expected by UpstoxBroker.load_instruments
        normalized.append({
            "symbol": symbol,
            "exchange": exchange,
            "key": str(row.get("instrument_key", f"{exchange}:{symbol}")),
            "asset_class": asset_class,
            "instrument_type": instrument_type or None,
            "right": right,
            "expiry": expiry_iso,
            "strike": strike_raw if strike_raw not in (None, "") else None,
            "underlying": underlying or None,
            "lot_size": lot_size,
            "master_status": status,
        })
    if strict and len(normalized) < MIN_MASTER_ROWS:
        raise SDKError(
            f"Upstox instrument master has too few rows "
            f"({len(normalized)} < {MIN_MASTER_ROWS}); refusing it as the master"
        )
    return normalized


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


_BROKER_PIPELINES: dict[str, tuple[Callable[..., Any], Callable[..., Any]]] = {
    "dhan": (dhan_master_download, parse_dhan_master),
    "upstox": (upstox_master_download, parse_upstox_master),
}


def load_instruments(fetch: Fetch, broker: str, *, strict: bool = True) -> list[Instrument]:
    """Download, parse, and build domain instruments for *broker*'s master.

    The single entry point of the instrument-loading pipeline: fetches the
    broker's master file, normalizes its rows, and builds typed domain
    ``Instrument`` objects. Registration into a broker's registry stays with
    ``BaseBroker.load_instruments`` — this returns the instrument list.
    """
    try:
        download, parse = _BROKER_PIPELINES[broker]
    except KeyError:
        raise ValueError(f"unknown broker for instrument loading: {broker!r}") from None
    raw = download(fetch, strict=strict)()
    return [build_instrument_from_row(row) for row in parse(raw, strict=strict)]


__all__ = [
    "DHAN_INSTRUMENT_MASTER_URL",
    "DHAN_SEGMENT_CANONICAL",
    "MIN_MASTER_ROWS",
    "UPSTOX_INSTRUMENT_MASTER_URL",
    "UPSTOX_SEGMENT_CANONICAL",
    "dhan_master_download",
    "load_instruments",
    "load_mcx_rows",
    "parse_dhan_master",
    "parse_upstox_master",
    "upstox_master_download",
]
