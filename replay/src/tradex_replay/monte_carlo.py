"""Monte Carlo simulation for backtest metrics confidence intervals."""

from __future__ import annotations

import random
from dataclasses import dataclass
from typing import Any

import statistics


@dataclass
class MonteCarloResult:
    mean_return: float
    std_return: float
    ci_lower: float  # 5th percentile
    ci_upper: float  # 95th percentile
    prob_profit: float
    num_simulations: int


def monte_carlo_returns(
    returns: list[float],
    n_simulations: int = 1000,
    horizon: int = 252,
    seed: int | None = None,
) -> MonteCarloResult:
    """Bootstrap resample returns to estimate future performance distribution."""
    rng = random.Random(seed) if seed is not None else random
    if not returns:
        return MonteCarloResult(0.0, 0.0, 0.0, 0.0, 0.0, 0)

    sim_returns = []
    for _ in range(n_simulations):
        sampled = [rng.choice(returns) for _ in range(horizon)]
        # Compound
        cum = 1.0
        for r in sampled:
            cum *= (1 + r)
        sim_returns.append(cum - 1.0)

    sim_returns.sort()
    mean_r = statistics.mean(sim_returns)
    std_r = statistics.stdev(sim_returns) if len(sim_returns) > 1 else 0.0
    ci_lower = sim_returns[int(0.05 * len(sim_returns))]
    ci_upper = sim_returns[int(0.95 * len(sim_returns))]
    prob_profit = sum(1 for r in sim_returns if r > 0) / len(sim_returns)

    return MonteCarloResult(
        mean_return=mean_r,
        std_return=std_r,
        ci_lower=ci_lower,
        ci_upper=ci_upper,
        prob_profit=prob_profit,
        num_simulations=n_simulations,
    )
