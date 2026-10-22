"""Unified rate-limit tables for all broker adapters.

Single source of truth for per-broker, per-bucket rate limits.  Each bucket row
carries ``rate_per_second``, ``capacity``, ``min_interval``,
``cooldown_seconds``, optional ``wait_cooldown``, and optional
``extra_windows`` — the shape ``limiter_from_table`` consumes.

The canonical names below are the single home for all broker rate tables.
"""

from __future__ import annotations

RateTable = dict[str, dict[str, float | int | tuple[tuple[int, float], ...]]]

DHAN_RATE_TABLE: RateTable = {
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

UPSTOX_RATE_TABLE: RateTable = {
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

PAPER_RATE_TABLE: RateTable = {
    "orders": {
        "rate_per_second": 1000.0,
        "capacity": 1000,
        "min_interval": 0.0,
        "cooldown_seconds": 0.0,
    },
    "quotes": {
        "rate_per_second": 1000.0,
        "capacity": 1000,
        "min_interval": 0.0,
        "cooldown_seconds": 0.0,
    },
    "historical": {
        "rate_per_second": 1000.0,
        "capacity": 1000,
        "min_interval": 0.0,
        "cooldown_seconds": 0.0,
    },
    "admin": {
        "rate_per_second": 1000.0,
        "capacity": 1000,
        "min_interval": 0.0,
        "cooldown_seconds": 0.0,
    },
}

RATE_TABLES: dict[str, RateTable] = {
    "dhan": DHAN_RATE_TABLE,
    "upstox": UPSTOX_RATE_TABLE,
    "paper": PAPER_RATE_TABLE,
}

