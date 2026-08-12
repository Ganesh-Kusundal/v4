"""Runtime utilities for the TradeX v4 trading platform.

Provides boot composition root, trading calendar, metrics, and live
broker construction.
"""

from tradex_trading.runtime.calendar import NSETradingCalendar
from tradex_trading.runtime.live import (
    build_broker_from_env,
    build_dhan_from_env,
    build_upstox_from_env,
    load_env_file,
    provider_environment,
    resolve_fetch,
)
from tradex_trading.runtime.metrics import MetricsRegistry
from tradex_trading.runtime.startup import RuntimeContext, boot, boot_context

__all__ = [
    "MetricsRegistry",
    "NSETradingCalendar",
    "RuntimeContext",
    "boot",
    "boot_context",
    "build_broker_from_env",
    "build_dhan_from_env",
    "build_upstox_from_env",
    "load_env_file",
    "provider_environment",
    "resolve_fetch",
]
