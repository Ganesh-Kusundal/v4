"""Tests for Monte Carlo."""

from tradex_replay.monte_carlo import monte_carlo_returns


def test_monte_carlo_basic():
    returns = [0.01, -0.005, 0.02, -0.01, 0.015, 0.005, -0.008]
    result = monte_carlo_returns(returns, n_simulations=100, horizon=10, seed=42)
    assert result.num_simulations == 100
    assert 0 <= result.prob_profit <= 1
    assert result.ci_lower <= result.ci_upper


def test_monte_carlo_deterministic():
    returns = [0.01, -0.005, 0.02]
    r1 = monte_carlo_returns(returns, n_simulations=50, seed=1)
    r2 = monte_carlo_returns(returns, n_simulations=50, seed=1)
    assert r1.mean_return == r2.mean_return


def test_monte_carlo_empty():
    result = monte_carlo_returns([])
    assert result.num_simulations == 0
