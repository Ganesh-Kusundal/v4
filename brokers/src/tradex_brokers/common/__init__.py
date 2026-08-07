"""Common infrastructure shared across all broker adapters.

Re-exports the key types so that adapters can do::

    from tradex_brokers.common import (
        CircuitBreaker,
        DurableTokenManager,
        ResiliencePipeline,
        TokenBucketRateLimiter,
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
from tradex_brokers.common.circuit_breaker import (
    CircuitBreaker,
    CircuitBreakerConfig,
    CircuitBreakerOpenError,
    CircuitState,
)
from tradex_brokers.common.instruments import (
    future_chain_from_master,
    load_master_csv,
    load_master_json,
)
from tradex_brokers.common.message_log import (
    InMemoryMessageLog,
    MessageEnvelope,
    MessageLog,
    SQLiteMessageLog,
)
from tradex_brokers.common.paths import (
    default_instrument_cache_path,
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
    verify_auth_connection,
)
from tradex_brokers.common.rate_limit import (
    BROKER_RATE_TABLES,
    DHAN_RATE_LIMITS,
    PAPER_RATE_LIMITS,
    UPSTOX_RATE_LIMITS,
    MultiBucketRateLimiter,
    RateLimitConfig,
    RollingWindowCounter,
    TokenBucketRateLimiter,
    bucket_for_path,
    limiter_for_provider,
    limiter_from_table,
    table_for_provider,
)
from tradex_brokers.common.resilience import ResiliencePipeline
from tradex_brokers.common.retry import (
    RetryableHttpClient,
    RetryConfig,
    RetryExhaustedError,
    retryable,
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
    # circuit breaker
    "CircuitBreaker",
    "CircuitBreakerConfig",
    "CircuitBreakerOpenError",
    "CircuitState",
    # instruments
    "future_chain_from_master",
    "load_master_csv",
    "load_master_json",
    # message log
    "InMemoryMessageLog",
    "MessageEnvelope",
    "MessageLog",
    "SQLiteMessageLog",
    # paths
    "default_totp_cooldown_path",
    "default_instrument_cache_path",
    "default_runtime_dir",
    "default_token_state_path",
    # provider client
    "ProviderHttpClient",
    "UncertainSubmissionTracker",
    # provider common
    "as_decimal",
    "parse_timestamp",
    "require_success",
    "verify_auth_connection",
    # rate limit
    "BROKER_RATE_TABLES",
    "DHAN_RATE_LIMITS",
    "MultiBucketRateLimiter",
    "PAPER_RATE_LIMITS",
    "RateLimitConfig",
    "RollingWindowCounter",
    "TokenBucketRateLimiter",
    "UPSTOX_RATE_LIMITS",
    "bucket_for_path",
    "limiter_for_provider",
    "limiter_from_table",
    "table_for_provider",
    # resilience
    "ResiliencePipeline",
    # retry
    "RetryConfig",
    "RetryExhaustedError",
    "RetryableHttpClient",
    "retryable",
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
