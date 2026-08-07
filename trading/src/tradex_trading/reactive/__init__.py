"""Reactive infrastructure for the TradeX v4 trading platform.

Provides RxPY-backed message bus, stream operators, backpressure presets,
and subscription lifecycle management.
"""

from tradex_trading.reactive.backpressure import BackpressurePresets
from tradex_trading.reactive.bus import ReactiveBus
from tradex_trading.reactive.subscription import DisposableSubscription, SubscriptionManager

__all__ = [
    "ReactiveBus",
    "DisposableSubscription",
    "SubscriptionManager",
    "BackpressurePresets",
]
