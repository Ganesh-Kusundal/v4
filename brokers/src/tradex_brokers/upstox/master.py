"""Upstox instrument-master download and row normalization.

The pipeline itself lives in ``tradex_brokers.common.instrument_loader``;
this module re-exports it so the historical import path
(``tradex_brokers.upstox.master``) keeps working for trading layers and tests.
"""

from __future__ import annotations

from tradex_brokers.common.instrument_loader import (
    MIN_MASTER_ROWS,
    UPSTOX_INSTRUMENT_MASTER_URL,
    parse_upstox_master,
    upstox_master_download,
)

__all__ = [
    "MIN_MASTER_ROWS",
    "UPSTOX_INSTRUMENT_MASTER_URL",
    "parse_upstox_master",
    "upstox_master_download",
]
