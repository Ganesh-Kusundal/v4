"""Configuration for the TradeX v4 trading platform.

Provides AppConfig schema and environment loading.
"""

from tradex_trading.config.env import _parse_bool, from_env
from tradex_trading.config.schema import (
    AppConfig,
    BrokerConfig,
    LoggingConfig,
    ObservabilityConfig,
    PersistenceConfig,
    RiskConfig,
)

__all__ = [
    "AppConfig",
    "BrokerConfig",
    "LoggingConfig",
    "ObservabilityConfig",
    "PersistenceConfig",
    "RiskConfig",
    "_parse_bool",
    "from_env",
]
