"""Configuration for the TradeX v4 trading platform.

Provides AppConfig schema and environment loading.
"""

from tradex_config.env import _parse_bool, from_env
from tradex_config.schema import (
    AppConfig,
    BrokerConfig,
    PersistenceConfig,
    RiskConfig,
)

__all__ = [
    "AppConfig",
    "BrokerConfig",
    "PersistenceConfig",
    "RiskConfig",
    "_parse_bool",
    "from_env",
]
