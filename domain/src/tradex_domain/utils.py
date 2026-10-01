"""Shared Decimal utilities for paisa-quantized arithmetic.

Pure math helpers used across fees, position accounting, and backtesting
to guarantee a single rounding convention everywhere.
"""

from __future__ import annotations

from decimal import ROUND_HALF_UP, Decimal, InvalidOperation


def q2(value: Decimal) -> Decimal:
    """Quantize to 2 decimal places (paisa) with ROUND_HALF_UP.

    This is the single source of truth for money rounding across the
    entire TradeX v4 codebase — fees, realized P&L, and backtest equity
    all use the same function so rounding never diverges between modes.
    """
    return value.quantize(Decimal("0.01"), rounding=ROUND_HALF_UP)


def as_decimal(value: str | float | int) -> Decimal:
    """Convert a value to ``Decimal`` safely.

    The single shared coercion helper (audit REF-5) — merged from the two
    byte-identical bodies that used to live in
    ``tradex_brokers.common.instruments`` (raising ``SDKError``) and
    ``tradex_shared.rate_limiting`` (raising ``ValueError``). Both originals
    handled exactly the same input space; only the exception type differed,
    so the ValueError contract here is a superset and the brokers layer
    re-wraps it in ``SDKError``.

    Handles ``Decimal`` passthrough, ints/floats (via ``str``), and strings
    with surrounding whitespace, thousands commas, and currency symbols
    (``₹``, ``$``). An empty or whitespace-only string reads as
    ``Decimal("0")``.

    Raises
    ------
    ValueError
        If the value cannot be converted.
    """
    if isinstance(value, Decimal):
        return value
    if isinstance(value, (int, float)):
        return Decimal(str(value))
    if isinstance(value, str):
        cleaned = value.strip().replace(",", "").replace("₹", "").replace("$", "")
        if not cleaned:
            return Decimal("0")
        try:
            return Decimal(cleaned)
        except InvalidOperation as exc:
            raise ValueError(f"Cannot convert {value!r} to Decimal") from exc
    raise ValueError(f"Cannot convert {type(value).__name__} to Decimal")


__all__ = ["as_decimal", "q2"]
