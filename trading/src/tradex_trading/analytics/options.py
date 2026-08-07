"""Options helpers: intrinsic value + Black-Scholes call."""

from __future__ import annotations

import math


def intrinsic_call(spot: float, strike: float) -> float:
    """Intrinsic value of a call option."""
    return max(spot - strike, 0.0)


def black_scholes_call(
    spot: float,
    strike: float,
    time_years: float,
    rate: float,
    sigma: float,
) -> float:
    """Black-Scholes European call price."""
    if time_years <= 0 or sigma <= 0:
        return intrinsic_call(spot, strike)
    sqrt_t = math.sqrt(time_years)
    d1 = (
        (math.log(spot / strike) + (rate + 0.5 * sigma * sigma) * time_years)
        / (sigma * sqrt_t)
    )
    d2 = d1 - sigma * sqrt_t
    return (
        spot * _norm_cdf(d1)
        - strike * math.exp(-rate * time_years) * _norm_cdf(d2)
    )


def _norm_cdf(x: float) -> float:
    return 0.5 * (1.0 + math.erf(x / math.sqrt(2.0)))


__all__ = ["black_scholes_call", "intrinsic_call"]
