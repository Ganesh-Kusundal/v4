"""Shared Decimal utilities for paisa-quantized arithmetic.

Pure math helpers used across fees, position accounting, and backtesting
to guarantee a single rounding convention everywhere.
"""

from __future__ import annotations

from decimal import ROUND_HALF_UP, Decimal


def q2(value: Decimal) -> Decimal:
    """Quantize to 2 decimal places (paisa) with ROUND_HALF_UP.

    This is the single source of truth for money rounding across the
    entire TradeX v4 codebase — fees, realized P&L, and backtest equity
    all use the same function so rounding never diverges between modes.
    """
    return value.quantize(Decimal("0.01"), rounding=ROUND_HALF_UP)


# Legacy alias kept for backward compatibility with private imports
# in ``fees.py`` and ``position_math.py`` (``from ... import _q2``).
_q2 = q2


__all__ = ["q2", "_q2"]
