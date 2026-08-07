"""Reactive infrastructure for the TradeX v4 trading platform.

Provides RxPY-backed message bus, typed stream operators, backpressure
presets, and subscription lifecycle management.
"""

from tradex_trading.reactive.backpressure import BackpressurePresets
from tradex_trading.reactive.bus import ReactiveBus
from tradex_trading.reactive.operators import (
    catch_error,
    distinct_until_changed,
    filter_safe,
    map_to,
    of_type,
    replay_buffer,
    sample,
    share,
    take_until,
    throttle_first,
)
from tradex_trading.reactive.subscription import (
    DisposableSubscription,
    SubscriptionManager,
)

__all__ = [
    "ReactiveBus",
    "DisposableSubscription",
    "SubscriptionManager",
    "BackpressurePresets",
    "of_type",
    "share",
    "replay_buffer",
    "throttle_first",
    "sample",
    "distinct_until_changed",
    "take_until",
    "map_to",
    "filter_safe",
    "catch_error",
]
