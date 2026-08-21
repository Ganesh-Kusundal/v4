"""Single-source market session calendar.

Owns the only copy of ``09:15`` / ``15:30`` and MCX ``09:00``/``23:30``
wall-clock bounds.  Previously this literal was triplicated in
``trading/runtime/calendar.py``, ``trading/datalake/parquet_storage.py`` and
``brokers/dhan/_marketdata.py`` — a textbook shotgun-surgery literal.

Ponytail: one file, stdlib only, no broker/trading import.
"""

from __future__ import annotations

from datetime import time

# NSE/BSE cash session (IST wall time)
MARKET_OPEN: time = time(9, 15)
MARKET_CLOSE: time = time(15, 30)

MARKET_OPEN_STR: str = "09:15:00"
MARKET_CLOSE_STR: str = "15:30:00"

# Per-exchange Dhan intraday session bounds (v2 parity).  Unknown exchange
# falls back to the cash defaults above.
DHAN_SESSION_OPEN: dict[str, str] = {"MCX_COMM": "09:00:00", "NSE_COMM": "09:00:00"}
DHAN_SESSION_CLOSE: dict[str, str] = {"MCX_COMM": "23:30:00", "NSE_COMM": "23:30:00"}

__all__ = [
    "DHAN_SESSION_CLOSE",
    "DHAN_SESSION_OPEN",
    "MARKET_CLOSE",
    "MARKET_CLOSE_STR",
    "MARKET_OPEN",
    "MARKET_OPEN_STR",
]
