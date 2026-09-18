"""Upstox rate-limit table.

Per-bucket rate limits tuned to Upstox's documented standard limits:
orders 10/s·500/min·2000/30min, standard APIs 50/s.

Every bucket can be overridden at runtime via ``UPSTOX_RATE_<BUCKET>`` env vars.
"""

from __future__ import annotations

UPSTOX_RATE_LIMITS: dict[str, dict[str, float | int | tuple[tuple[int, float], ...]]] = {
    "orders": {
        "rate_per_second": 10.0,
        "capacity": 20,
        "min_interval": 0.1,
        "cooldown_seconds": 60.0,
        "extra_windows": ((500, 60.0), (2000, 1800.0)),
    },
    "quotes": {
        "rate_per_second": 25.0,
        "capacity": 50,
        "min_interval": 0.04,
        "cooldown_seconds": 60.0,
    },
    "historical": {
        # Upstox documented limit (upstox.com/developer/api-documentation/rate-limiting):
        # Historical Candles = 50 req/s, 500/min, 2000/30min. The per-second cap
        # is NOT the binding one for a bulk sync — 500/min (8.3/s) is. capacity
        # 80 -> 50 to match the documented per-second burst (80 exceeded it).
        # ponytail: the 500/min + 2000/30min caps are NOT enforced here —
        # extra_windows fails fast (returns False) instead of throttling, so a
        # long Upstox backfill must self-pace or gain window-aware retry first.
        "rate_per_second": 50.0,
        "capacity": 50,
        "min_interval": 0.02,
        "cooldown_seconds": 60.0,
        "wait_cooldown": True,
    },
    "option_chain": {
        "rate_per_second": 50.0,
        "capacity": 100,
        "min_interval": 0.02,
        "cooldown_seconds": 60.0,
    },
    "funds": {
        "rate_per_second": 50.0,
        "capacity": 100,
        "min_interval": 0.02,
        "cooldown_seconds": 60.0,
    },
    "positions": {
        "rate_per_second": 50.0,
        "capacity": 100,
        "min_interval": 0.02,
        "cooldown_seconds": 60.0,
    },
    "holdings": {
        "rate_per_second": 50.0,
        "capacity": 100,
        "min_interval": 0.02,
        "cooldown_seconds": 60.0,
    },
    "options_historical": {
        "rate_per_second": 50.0,
        "capacity": 100,
        "min_interval": 0.02,
        "cooldown_seconds": 60.0,
    },
    "expired_historical": {
        "rate_per_second": 50.0,
        "capacity": 100,
        "min_interval": 0.02,
        "cooldown_seconds": 60.0,
    },
    "admin": {
        "rate_per_second": 50.0,
        "capacity": 100,
        "min_interval": 0.02,
        "cooldown_seconds": 60.0,
    },
}
