"""Paper broker rate-limit table.

Effectively unlimited — the paper broker is in-process with no real API.
"""

from __future__ import annotations

from tradex_brokers.common.rate_table import PAPER_RATE_TABLE as PAPER_RATE_LIMITS

__all__ = ["PAPER_RATE_LIMITS"]
