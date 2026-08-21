"""Single-source broker REST base URLs (audit SMELL-01).

Owns the only copies of the Dhan/Upstox endpoint literals. Previously these
were hardcoded in ``dhan/client.py``, ``dhan/adapter.py``, ``upstox/client.py``
(×2), ``upstox/adapter.py`` and re-defaulted in ``trading/runtime/live.py`` —
six sites for one logical value.
"""

from __future__ import annotations

DHAN_REST_BASE_URL = "https://api.dhan.co/v2"
DHAN_SANDBOX_REST_BASE_URL = "https://sandbox.dhan.co/v2"

UPSTOX_REST_BASE_URL = "https://api.upstox.com/v2"
UPSTOX_HFT_BASE_URL = "https://api-hft.upstox.com/v3"
UPSTOX_V3_BASE_URL = "https://api.upstox.com/v3"

__all__ = [
    "DHAN_REST_BASE_URL",
    "DHAN_SANDBOX_REST_BASE_URL",
    "UPSTOX_HFT_BASE_URL",
    "UPSTOX_REST_BASE_URL",
    "UPSTOX_V3_BASE_URL",
]
