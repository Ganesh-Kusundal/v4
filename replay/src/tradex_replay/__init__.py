"""Replay module — deterministic event replay and backtesting."""

from tradex_replay.backtest import (
    BacktestEngine,
    BacktestResult,
    adv_provider_from_candles,
)
from tradex_replay.synthetic_ticks import SyntheticTickGenerator

__all__ = [
    "BacktestEngine",
    "BacktestResult",
    "SyntheticTickGenerator",
    "adv_provider_from_candles",
]

