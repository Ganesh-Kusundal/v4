"""Configuration for the TradeX v4 trading platform.

Provides AppConfig schema and environment loading.
"""

from tradex_config.env import _parse_bool, from_env
from tradex_config.permissions import (
    InsecureSecretFileError,
    assert_secret_file_mode,
    check_secret_files,
)
from tradex_config.broker_limits import BROKER_LIMITS, BrokerIntradayLimits
from tradex_config.schema import (
    AppConfig,
    BrokerConfig,
    PersistenceConfig,
    RiskConfig,
)

__all__ = [
    "AppConfig",
    "BrokerConfig",
    "BrokerIntradayLimits",
    "BROKER_LIMITS",
    "InsecureSecretFileError",
    "PersistenceConfig",
    "RiskConfig",
    "_parse_bool",
    "assert_secret_file_mode",
    "check_secret_files",
    "from_env",
]
