"""Upstox instrument master loader.

The segment map lives in ``tradex_brokers.common.instrument_loader``;
this module re-exports it under the historical name ``SEGMENT_CANONICAL``.
"""

from __future__ import annotations

from tradex_brokers.common.instrument_loader import (
    UPSTOX_SEGMENT_CANONICAL as SEGMENT_CANONICAL,
)

__all__ = ["SEGMENT_CANONICAL"]
