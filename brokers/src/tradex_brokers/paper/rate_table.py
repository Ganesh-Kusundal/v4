"""Paper broker rate-limit table.

Effectively unlimited — the paper broker is in-process with no real API.
"""

from __future__ import annotations

PAPER_RATE_LIMITS: dict[str, dict[str, float | int | tuple[tuple[int, float], ...]]] = {
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
