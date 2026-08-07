"""Upstox instrument master CSV loader.

Loads the Upstox instrument master file and normalizes rows into domain
Instrument objects.
"""

from __future__ import annotations

from pathlib import Path

# Segment -> canonical v4 exchange. Single source of truth for Upstox
# master-row exchange normalization (consumed by ``tradex_brokers.upstox.master``).
# Includes the BSE/NCDEX/COM variants the trading layer previously re-declared.
SEGMENT_CANONICAL: dict[str, str] = {
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


def load_upstox_rows(path: Path | None = None) -> list[dict]:
    """Load Upstox instrument master CSV. Returns empty list if no path."""
    if path is None:
        return []
    from tradex_brokers.common.instruments import load_master_csv

    return load_master_csv(path)


__all__ = ["SEGMENT_CANONICAL", "load_upstox_rows"]
