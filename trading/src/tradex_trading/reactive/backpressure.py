"""Backpressure presets for market data streams.

Pre-configured RxPY operator compositions for common trading-stream patterns.
"""

from __future__ import annotations

from rx import operators as ops


class BackpressurePresets:
    """Pre-configured backpressure strategies for common market data patterns."""

    @staticmethod
    def quote_throttle(period_ms: int = 100):
        """Throttle quote stream — emit at most once per *period_ms*.

        .. warning:: Only use on market data streams, NEVER on order streams.
        """
        return ops.throttle_first(period_ms)

    @staticmethod
    def depth_sample(period_ms: int = 200):
        """Sample depth stream at regular intervals.

        .. warning:: Only use on market data streams, NEVER on order streams.
        """
        return ops.sample(period_ms)

    @staticmethod
    def candle_buffer(count: int = 5):
        """Buffer candles into batches of *count* for batch processing."""
        return ops.buffer_with_count(count)

    @staticmethod
    def tick_window(time_ms: int = 1000):
        """Window ticks into time-based windows of *time_ms* for aggregation."""
        return ops.window_with_time(time_ms)

    @staticmethod
    def order_passthrough():
        """No backpressure for order streams — every order matters.

        This is the ONLY safe strategy for order/fill streams.
        """
        return ops.share()
