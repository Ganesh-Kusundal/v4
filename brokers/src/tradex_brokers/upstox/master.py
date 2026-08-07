"""Upstox instrument-master download and row normalization.

The Upstox master is a gzipped JSON array fetched from
``assets.upstox.com``; each row carries an ``exchange`` + ``segment`` pair
that maps to a canonical v4 exchange via ``SEGMENT_CANONICAL``. Normalized
rows are what ``UpstoxBroker.load_instruments`` consumes. Trading layers
orchestrate caching around these primitives (``MasterFileCache`` /
``MasterLoader``) but never parse broker formats themselves.
"""

from __future__ import annotations

import gzip
import json
from collections.abc import Callable
from datetime import UTC, datetime
from typing import Any

from tradex_domain import SDKError
from tradex_domain.value_objects import InstrumentId

from tradex_brokers.common.transport import Fetch
from tradex_brokers.upstox.instruments import SEGMENT_CANONICAL

UPSTOX_INSTRUMENT_MASTER_URL = (
    "https://assets.upstox.com/market-quote/instruments/exchange/complete.json.gz"
)

# Real masters carry 100k+ rows (Upstox ~150k). A strict-mode download below
# this floor is a corrupt/truncated fetch — never trust it as the instrument
# master (v2 MIN_*_INSTRUMENTS parity). Injected-fetch paths (tests) run
# non-strict and keep small fixtures working.
MIN_MASTER_ROWS = 10_000


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
        exchange = SEGMENT_CANONICAL.get(segment, exchange_raw)
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
        })
    if strict and len(normalized) < MIN_MASTER_ROWS:
        raise SDKError(
            f"Upstox instrument master has too few rows "
            f"({len(normalized)} < {MIN_MASTER_ROWS}); refusing it as the master"
        )
    return normalized


__all__ = [
    "MIN_MASTER_ROWS",
    "UPSTOX_INSTRUMENT_MASTER_URL",
    "parse_upstox_master",
    "upstox_master_download",
]
