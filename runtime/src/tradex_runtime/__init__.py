"""Runtime utilities for the TradeX v4 trading platform.

Provides boot composition root, trading calendar, metrics, and live
broker construction.
"""

from tradex_runtime.calendar import NSETradingCalendar
from tradex_runtime.live import (
    build_broker_from_env,
    build_dhan_from_env,
    build_upstox_from_env,
    load_env_file,
    provider_environment,
    resolve_fetch,
)
from tradex_runtime.metrics import MetricsRegistry
from tradex_runtime.session import SessionState, TradingSession
from tradex_runtime.startup import RuntimeContext, boot, boot_context
from tradex_runtime.streaming import BackendStreamSubscription, StreamSubscription

__all__ = [
    "BackendStreamSubscription",
    "MetricsRegistry",
    "NSETradingCalendar",
    "RuntimeContext",
    "SessionState",
    "StreamSubscription",
    "TradingSession",
    "boot",
    "boot_context",
    "build_broker_from_env",
    "build_dhan_from_env",
    "build_upstox_from_env",
    "load_env_file",
    "provider_environment",
    "resolve_fetch",
]
