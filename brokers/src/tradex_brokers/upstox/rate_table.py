"""Upstox rate-limit table.

Per-bucket rate limits tuned to Upstox's documented standard limits:
orders 10/s·500/min·2000/30min, standard APIs 50/s.

Every bucket can be overridden at runtime via ``UPSTOX_RATE_<BUCKET>`` env vars.
"""

from __future__ import annotations

from tradex_brokers.common.rate_table import UPSTOX_RATE_TABLE as UPSTOX_RATE_LIMITS

__all__ = ["UPSTOX_RATE_LIMITS"]
