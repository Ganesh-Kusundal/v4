"""Runtime utilities for the TradeX v4 trading platform.

Provides boot composition root, trading calendar, metrics, and live
broker construction.
"""

from tradex_domain.market_calendar import NSETradingCalendar
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
from tradex_runtime.startup import boot
from tradex_runtime.clocks import LiveClock, ReplayClock, SimClock
from tradex_runtime.environments import EnvironmentFactory, EnvironmentType, TradingEnvironment
from tradex_runtime.streaming import BackendStreamSubscription, StreamSubscription

__all__ = [
    "BackendStreamSubscription",
    "MetricsRegistry",
    "NSETradingCalendar",
    "SessionState",
    "StreamSubscription",
    "TradingSession",
    "boot",
    "build_broker_from_env",
    "build_dhan_from_env",
    "build_upstox_from_env",
    "load_env_file",
    "provider_environment",
    "resolve_fetch",
]
