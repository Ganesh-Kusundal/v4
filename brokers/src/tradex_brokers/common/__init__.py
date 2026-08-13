"""Common infrastructure shared across all broker adapters.

Re-exports the key types so that adapters can do::

    from tradex_brokers.common import (
        DurableTokenManager,
        TotpCooldownGuard,
        ...
    )
"""

from tradex_brokers.common.auth import (
    dhan_totp_mint,
    jwt_expiry,
    totp_code,
    upstox_refresh_mint,
    upstox_totp_mint,
)
from tradex_brokers.common.cache import ReadCache
from tradex_brokers.common.instruments import future_chain_from_master
from tradex_brokers.common.paths import (
    default_runtime_dir,
    default_token_state_path,
    default_totp_cooldown_path,
)
from tradex_brokers.common.provider_client import (
    ProviderHttpClient,
    UncertainSubmissionTracker,
)
from tradex_brokers.common.provider_common import (
    as_decimal,
    parse_timestamp,
    require_success,
)
from tradex_brokers.common.resilience import (
    CircuitBreaker,
    MultiBucketRateLimiter,
    ResiliencePipeline,
    RetryableHttpClient,
    TokenBucketRateLimiter,
    bucket_for_path,
    limiter_for_provider,
)
from tradex_brokers.common.streaming import ReconnectingStreamBackend
from tradex_brokers.common.token_lifecycle import (
    DurableTokenManager,
    MintStrategy,
    MintTokenManager,
    PortTokenManager,
    TokenBroadcast,
    TokenLifecyclePort,
    TokenMintResult,
    TokenRefreshScheduler,
)
from tradex_brokers.common.totp_cooldown import (
    BROKER_COOLDOWN_SECONDS,
    DHAN_COOLDOWN_SECONDS,
    UPSTOX_COOLDOWN_SECONDS,
    TotpCooldownGuard,
    TotpRateLimitError,
)
from tradex_brokers.common.transport import Fetch, HttpTransport
from tradex_brokers.common.ws_reconnect import (
    AutoReconnectMixin,
    ReconnectConfig,
    WSReconnectManager,
    WsReconnectManager,
)

__all__ = [
    # auth
    "dhan_totp_mint",
    "jwt_expiry",
    "totp_code",
    "upstox_refresh_mint",
    "upstox_totp_mint",
    # cache
    "ReadCache",
    # instruments
    "future_chain_from_master",
    # paths
    "default_totp_cooldown_path",
    "default_runtime_dir",
    "default_token_state_path",
    # provider client
    "ProviderHttpClient",
    "UncertainSubmissionTracker",
    # provider common
    "as_decimal",
    "parse_timestamp",
    "require_success",
    # resilience
    "CircuitBreaker",
    "MultiBucketRateLimiter",
    "ResiliencePipeline",
    "RetryableHttpClient",
    "TokenBucketRateLimiter",
    "bucket_for_path",
    "limiter_for_provider",
    # streaming
    "ReconnectingStreamBackend",
    # token lifecycle
    "DurableTokenManager",
    "MintStrategy",
    "MintTokenManager",
    "PortTokenManager",
    "TokenBroadcast",
    "TokenLifecyclePort",
    "TokenMintResult",
    "TokenRefreshScheduler",
    # totp cooldown
    "BROKER_COOLDOWN_SECONDS",
    "DHAN_COOLDOWN_SECONDS",
    "TotpCooldownGuard",
    "TotpRateLimitError",
    "UPSTOX_COOLDOWN_SECONDS",
    # transport
    "Fetch",
    "HttpTransport",
    # ws reconnect
    "ReconnectConfig",
    "AutoReconnectMixin",
    "WSReconnectManager",
    "WsReconnectManager",
]
