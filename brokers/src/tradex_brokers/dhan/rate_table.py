"""Dhan rate-limit table.

Per-bucket rate limits tuned to Dhan's documented standard limits:
orders 10/s·250/min·1000/hr·7000/day, data 5/s, quotes 1/s, non-trading 20/s.

Every bucket can be overridden at runtime via ``DHAN_RATE_<BUCKET>`` env vars
(e.g. ``DHAN_RATE_OPTION_CHAIN="1,2,1.0,60"``).
"""

from __future__ import annotations

DHAN_RATE_LIMITS: dict[str, dict[str, float | int | tuple[tuple[int, float], ...]]] = {
    "orders": {
        "rate_per_second": 10.0,
        "capacity": 20,
        "min_interval": 0.1,
        "cooldown_seconds": 130.0,
        "extra_windows": ((250, 60.0), (1000, 3600.0), (7000, 86400.0)),
    },
    "quotes": {
        "rate_per_second": 1.0,
        "capacity": 2,
        "min_interval": 1.0,
        "cooldown_seconds": 130.0,
    },
    "historical": {
        # DhanHQ documented limit (docs.dhanhq.co/api/v2/guides/rate-limits):
        # Data/Historical = 5 req/s, per-minute/hour unlimited, 100k/day, and a
        # 429 body that literally says "retry after 1 second".
        # ponytail: three fixes taken straight from that doc —
        #   cooldown 10s -> 1s: acquire() sleeps the FULL cooldown and it's
        #     global, so every 429 froze ALL workers for 10s. Dhan needs 1s.
        #     This — not the per-second rate — is why 2000 calls took ~35 min.
        #   rate 5 -> 4/s: at exactly 5/s, timing jitter lands 6 requests in a
        #     rolling 1s window -> 429. 4/s keeps a 20% margin, never breaches.
        #   capacity 200 -> 5: min_interval already gates to the rate, so 200
        #     was meaningless. 5 = one second of burst, matching the 5/s cap.
        "rate_per_second": 4.0,
        "capacity": 5,
        "min_interval": 0.25,
        "cooldown_seconds": 1.0,
        "wait_cooldown": True,
    },
    "options_historical": {
        "rate_per_second": 2.0,
        "capacity": 3,
        "min_interval": 0.5,
        "cooldown_seconds": 130.0,
        "wait_cooldown": True,
    },
    "expired_historical": {
        "rate_per_second": 5.0,
        "capacity": 10,
        "min_interval": 0.2,
        "cooldown_seconds": 60.0,
        "wait_cooldown": True,
    },
    "option_chain": {
        "rate_per_second": 0.34,
        "capacity": 1,
        "min_interval": 3.0,
        "cooldown_seconds": 130.0,
    },
    "admin": {
        "rate_per_second": 20.0,
        "capacity": 40,
        "min_interval": 0.05,
        "cooldown_seconds": 130.0,
    },
}
