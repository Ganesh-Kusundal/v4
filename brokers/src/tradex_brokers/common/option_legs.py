"""Normalized option-chain leg dicts shared by broker parsers.

Both Dhan and Upstox option-chain payloads carry per-leg premium, OI,
volume, IV and greeks under different key names. :func:`option_leg` builds
the one normalized dict the domain ``OptionPair.call_leg``/``put_leg``
fields carry: ``None`` values are omitted, ``provider`` is stamped, and a
leg with nothing parseable becomes ``None`` — chain parsing never raises on
leg metadata.
"""

from __future__ import annotations

from collections.abc import Mapping
from typing import Any


def opt_float(value: object) -> float | None:
    """Best-effort float for broker payload values; ``None`` when absent."""
    if value is None or value == "":
        return None
    try:
        return float(value)  # type: ignore[arg-type]
    except (TypeError, ValueError):
        return None


def option_leg(provider: str, fields: Mapping[str, Any]) -> dict[str, object] | None:
    """Normalize one leg's fields; ``None`` when every value is absent."""
    leg = {key: value for key, value in fields.items() if value is not None}
    if not leg:
        return None
    leg["provider"] = provider
    return leg


def greeks_block(greeks: object) -> dict[str, float] | None:
    """``{delta, gamma, theta, vega}`` floats from a broker greeks payload."""
    if not isinstance(greeks, Mapping):
        return None
    block = {
        key: value
        for key, value in (
            ("delta", opt_float(greeks.get("delta"))),
            ("gamma", opt_float(greeks.get("gamma"))),
            ("theta", opt_float(greeks.get("theta"))),
            ("vega", opt_float(greeks.get("vega"))),
        )
        if value is not None
    }
    return block or None
