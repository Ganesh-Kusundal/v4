"""Dhan instrument-master download and row normalization.

The pipeline itself lives in ``tradex_brokers.common.instrument_loader``;
this module re-exports it so the historical import path
(``tradex_brokers.dhan.master``) keeps working for trading layers and tests.
"""

from __future__ import annotations

from tradex_brokers.common.instrument_loader import (
    DHAN_INSTRUMENT_MASTER_URL,
    MIN_MASTER_ROWS,
    dhan_master_download,
    parse_dhan_master,
)

__all__ = [
    "DHAN_INSTRUMENT_MASTER_URL",
    "MIN_MASTER_ROWS",
    "dhan_master_download",
    "parse_dhan_master",
]
