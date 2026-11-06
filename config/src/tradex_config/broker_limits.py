"""Broker-specific limits and constraints.

Centralizes broker-specific configuration constants like intraday day limits
to avoid scattered magic values across the codebase.
"""

from dataclasses import dataclass
from datetime import time


@dataclass(frozen=True)
class BrokerIntradayLimits:
    """Intraday request day limits per broker.

    Dhan and Upstox have different caps on how many days of intraday data
    can be fetched in a single request. These limits are enforced by the
    ParallelHistoryFetcher's auto-chunking logic.

    ``dhan_last_1m`` is the last NSE 1-minute stamp ``/charts/intraday``
    actually returns. Minutes after it are not a hole sync can fill.
    """

    dhan_max_days: int = 90
    upstox_max_days: int = 30
    dhan_last_1m: time = time(15, 14)


# Singleton instance for easy import
BROKER_LIMITS = BrokerIntradayLimits()
