"""Dhan rate-limit table.

Per-bucket rate limits tuned to Dhan's documented standard limits:
orders 10/s·250/min·1000/hr·7000/day, data 5/s, quotes 1/s, non-trading 20/s.

Every bucket can be overridden at runtime via ``DHAN_RATE_<BUCKET>`` env vars
(e.g. ``DHAN_RATE_OPTION_CHAIN="1,2,1.0,60"``).
"""

from __future__ import annotations

from tradex_brokers.common.rate_table import DHAN_RATE_TABLE as DHAN_RATE_LIMITS

__all__ = ["DHAN_RATE_LIMITS"]
