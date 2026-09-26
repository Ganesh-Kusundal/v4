"""Dhan instrument-master download and row normalization.

The Dhan master is a CSV fetched from ``images.dhan.co``; its rows carry
exchange/segment codes that map to canonical v4 exchanges via
``SEGMENT_CANONICAL``. Normalized rows are what ``DhanBroker.load_instruments``
consumes (symbol/exchange/key/asset_class plus derivative structure for
futures/options). Trading layers orchestrate caching around these primitives
(``MasterFileCache`` / ``MasterLoader``) but never parse broker formats
themselves.
"""

from __future__ import annotations

import csv
import io
from collections.abc import Callable
from typing import Any

from tradex_domain import SDKError

from tradex_brokers.common.transport import Fetch
from tradex_brokers.dhan.instruments import SEGMENT_CANONICAL

DHAN_INSTRUMENT_MASTER_URL = "https://images.dhan.co/api-data/api-scrip-master.csv"

# Real masters carry 100k+ rows (Dhan ~220k). A strict-mode download below
# this floor is a corrupt/truncated fetch — never trust it as the instrument
# master (v2 MIN_*_INSTRUMENTS parity). Injected-fetch paths (tests) run
# non-strict and keep small fixtures working.
MIN_MASTER_ROWS = 10_000


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
        canonical = SEGMENT_CANONICAL.get((exchange, segment), exchange)
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
        })
    return normalized


__all__ = [
    "DHAN_INSTRUMENT_MASTER_URL",
    "MIN_MASTER_ROWS",
    "dhan_master_download",
    "parse_dhan_master",
]
