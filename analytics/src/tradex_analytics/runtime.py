"""Analytics runtime entry point.

The composition root lives in :mod:`tradex_analytics.views` alongside the view
it hands out; this module is the stable import path for the runtime itself.
"""

from __future__ import annotations

from tradex_analytics.views import AnalyticsRuntime, AnalyticsView, CachedAnalyticsView

__all__ = ["AnalyticsRuntime", "AnalyticsView", "CachedAnalyticsView"]
