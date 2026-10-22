"""Central constants for TradeX v4.

Single source of truth for all shared literals across the codebase.
Eliminates 300+ scattered constant definitions.
"""

from decimal import Decimal

DEFAULT_INITIAL_CAPITAL = Decimal("100000")
DEFAULT_BPS_DIVISOR = Decimal("10000")
DEFAULT_FILL_DEDUP_LRU = 50_000
DEFAULT_TICK_SIZE = Decimal("0.05")
DEFAULT_STOP_GAP_BPS = 25
DEFAULT_BACKTEST_RISK_FRACTION = 0.01
DEFAULT_WARMUP_BARS = 21
EMBARGO_FRACTION = 0.01
VALUE_AREA_PERCENT = 0.7
CCI_CONSTANT = 0.015
TRADING_DAYS_PER_YEAR = 252
EULER_MASCHERONI = 0.5772156649015329
BB_EXPANSION_LENGTH = 125
ALMA_OFFSET = 0.85
ALMA_SIGMA = 6.0
T3_FACTOR = 0.7
DECISION_POINT = "09:45"
BARS_PER_DAY = 25
CRYPTO_BARS_PER_DAY = 96

MAX_LOG_SIZE = 10_000
STOP_TIMEOUT = 5.0
SCHEDULER_INTERVAL = 86_400.0
RECONCILIATION_INTERVAL = 30.0
CADENCE_WINDOW = 180.0
ORDER_FEED_STALE_AFTER = 60.0
LEDGER_SIZE = 8192
FORMING_FRAME_MIN_INTERVAL = 1.0
LATE_TICK_REVISION_SECONDS = 60
DEDUP_WINDOW = 10_000
MAX_TAPE_BYTES = 64 * 1024 * 1024
MASTER_TTL_HOURS = 6.0
MASTER_CLEANUP_DAYS = 7
REFRESH_BUFFER_SECONDS = 1800.0
DHAN_REFRESH_BUFFER_MINUTES = 10
FETCH_TIMEOUT = 30.0
IMPERSONATE = "chrome131"
REDIRECT_URI = "http://127.0.0.1:18080/callback"
PAPER_CASH = Decimal("1000000000")
PAPER_SPREAD_BPS = 5
PAPER_LEVEL_DEPTH = 100000
PAPER_LEVELS = 5
PAPER_TICK_SIZE = Decimal("0.05")
BACKTEST_SLIPPAGE_BPS = 5
DEFAULT_LOOKBACK = 10
CACHE_TTL_SECONDS = 30.0
TAIL_LIMIT = 400
TICK_THROTTLE_SECONDS = 1.0
POINTS_PER_PUSH = 2
TAIL_CACHE_MAX_ENTRIES = 32
TAIL_CACHE_MAX_AGE_SECONDS = 900.0
TAIL_LOAD_JOIN_SECONDS = 5.0
RATE_LIMIT_MAX = 20
RATE_LIMIT_WINDOW = 60
_SAFE_METHODS = frozenset({"GET", "HEAD", "OPTIONS"})
_SUCCESS_STATUS = frozenset(range(200, 300))
SESSION_ABSOLUTE_TTL = 12
SESSION_IDLE_TTL = 2
SESSION_ID_BYTES = 32
CSRF_PURPOSE = b"csrf-v1"
OUTBOUND_QUEUE_MAX = 1024
CONTROL_QUEUE_MAX = 4096
WS_CLOSE_CONTROL_OVERFLOW = 1013
WS_CLOSE_POLICY_VIOLATION = 1008
WS_CLOSE_INTERNAL_ERROR = 1011
FEED_STATUS_POLL_SECONDS = 2.0
HISTORY_CACHE_MAXSIZE = 256
LRU_CACHE_MAXSIZE = 4
TAIL_LOOKBACK_DAYS = 30
REPLAY_LOOKBACK_DAYS = 3
MIN_SPEED = 0.01
MAX_SPEED = 1000.0
MIN_TICKS_PER_BAR = 2
MAX_TICKS_PER_BAR = 1000
MIN_MINUTES = 1
MAX_MINUTES = 10080
MAX_START_TIME = 10**12
SLEEP_INTERVAL = 0.01
DEFAULT_LIMIT = 50
MAX_LIMIT = 20000
PAPER_SNAPSHOT_SPREAD = Decimal("0.05")
PAPER_SNAPSHOT_LOOKBACK = 2
PAPER_LTP_CHECK = 100.0
DEFAULT_INTERVAL_STREAM = "1m"
DEFAULT_INTERVAL_CHART = "5m"
DEFAULT_UNIVERSE = "nifty50"
MAX_RESULTS_SMALL = 200
MAX_RESULTS_LARGE = 500
MAX_LIMIT_LARGE = 20000
BACKTEST_LOOKBACK_DAYS = 1
DEFAULT_HOST = "127.0.0.1"
DEFAULT_PORT = 8080
DEFAULT_WORKERS = 4
DEFAULT_BATCH_SIZE = 20
DEFAULT_MIN_GAP_STAMPS = 15
DEFAULT_BACKOFF_BASE = 60.0
DEFAULT_BACKOFF_MAX = 600.0
DEFAULT_SYNC_START_DAYS = 30
BAR_FREQ_MAP = {"1m": "1min", "5m": "5min", "15m": "15min"}
VERSION_DEFAULT = "1.0.0"
DEFAULT_WINDOW_DAYS = 30
MIN_WINDOW_DAYS = 5
MAX_WINDOW_DAYS = 120
ADOPTION_TOLERANCE = Decimal("0.01")
FUNDS_DRIFT_THRESHOLD = Decimal("1")
RECONCILIATION_TOLERANCE = Decimal("0.01")

NO_SESSION_BOUND = "no session bound"
FEED_NOT_READY = "feed_not_ready"
TAPE_RECORD_ENV_VAR = "TRADEX_RECORD_TAPE"
DHAN_COOLDOWN_SECONDS = 120.0
UPSTOX_COOLDOWN_SECONDS = 600.0
MIN_MASTER_ROWS = 10_000
DHAN_EQUITY_UNIVERSE = ("RELIANCE", "TCS", "INFY", "HDFCBANK")
UPSTOX_EQUITY_UNIVERSE = ("RELIANCE", "TCS", "INFY", "HDFCBANK")
SEGMENT_CANONICAL_DHAN = {
    ("NSE", "E"): "NSE",
    ("NSE", "D"): "NFO",
    ("NSE", "I"): "IDX",
    ("NSE", "C"): "CDS",
    ("NSE", "M"): "NSE_COMM",
    ("BSE", "E"): "BSE",
    ("BSE", "D"): "BFO",
    ("BSE", "I"): "IDX",
    ("BSE", "C"): "BCD",
    ("MCX", "M"): "MCX",
    ("IDX", "I"): "IDX",
    ("CDS", "D"): "CDS",
}
SEGMENT_CANONICAL_UPSTOX = {
    "NSE_EQ": "NSE",
    "BSE_EQ": "BSE",
    "NSE_FO": "NFO",
    "BSE_FO": "BFO",
    "MCX_FO": "MCX",
    "NSE_COM": "NSE_COMM",
    "BCD_FO": "BCD",
    "NCD_FO": "CDS",
    "NSE_CURRENCY": "CDS",
    "BSE_CURRENCY": "BCD",
    "NSE_INDEX": "IDX",
    "BSE_INDEX": "IDX",
}
PAPER_DEFAULT = "PAPER"
MODES = {"paper", "backtest", "replay", "live"}
READY_STATE = "READY"
DEPTH_MODES = {"off", "20", "30"}
RECOVERY_TRIGGERS = {"large_jump", "missing_bar"}
BOOK_MATCHING = "book_matching"
SLIPPAGE_MODEL = "slippage_model"
FILL_PATHS = {"book_matching", "slippage_model"}
AVG_PRICE_FALLBACK_PREFIX = "avg-price:"
BOOKED_FILL_REASON = "booked_fill"
WINDOW_EXPIRED_REASON = "window_expired"
STALE_REASON = "stale"
LARGE_JUMP_TRIGGER = "large_jump"
MISSING_BAR_TRIGGER = "missing_bar"
ORDERS_STREAM = "orders"
SYSTEM_STREAM = "system"
UNKNOWN_STATUS = "UNKNOWN"
HIGH_SEVERITY = "HIGH"
CRITICAL_SEVERITY = "CRITICAL"
LOW_SEVERITY = "LOW"
DRIFT_KINDS = {"position", "order", "funds"}
QUANTITY_MISMATCH_REASON = "quantity mismatch"
STATUS_REASON = "status"
MISSING_REASON = "missing"
BALANCE_ATTRIBUTE = "balance"
EQUITY_ATTRIBUTE = "equity"
PASSED_STATUS = "passed"
NOT_PROMOTED_STATUS = "not_promoted"
NOT_EVALUATED_STATUS = "not_evaluated"
PROMOTABLE_STATUS = "promotable"
NOT_PROMOTABLE_STATUS = "not_promotable"
UNREADABLE_POSITION_REASON = "unreadable_position"
UNREADABLE_QUANTITY_REASON = "unreadable_quantity"
UNREADABLE_INSTRUMENT_REASON = "unreadable_instrument"
UNSETTLED_FILL_REASON = "unsettled_fill"
VENUE_OMITTED_REASON = "venue_omitted"
UNSETTLED_GUARD_UNAVAILABLE_REASON = "unsettled_guard_unavailable"
VENUE_READ_FAILED_REASON = "venue_read_failed"
VENUE_READ_PARTIAL_REASON = "venue_read_partial"
VENUE_READ_STALE_REASON = "venue_read_stale"
VENUE_GET_POSITIONS_FAILED_REASON = "venue_get_positions_failed"
UNREADABLE_INSTRUMENT_PLACEHOLDER = "<unreadable:instrument>"
IDEMPOTENCY_REPLAY = "idempotency_replay"
IDEMPOTENCY_IN_FLIGHT = "idempotency_in_flight"
IDEMPOTENCY_KEY_HEADER = "Idempotency-Key"
SUBMITTED_STATUS = "SUBMITTED"
MODIFIED_STATUS = "modified"
CANCELLED_STATUS = "cancelled"
SERVICE_NAME = "tradex"
CLI_PROG_NAME = "tradex-v4"
CLI_DESCRIPTION = "TradeX v4 Trading Platform"
FASTAPI_TITLE = "TradeX v4 API"
FASTAPI_VERSION = "0.1.0"
SYNC_DHAN_ONLY_MESSAGE = "sync is Dhan-only"
SYNC_OUTPUT_LABELS = {
    "renamed": "Renamed:",
    "quarantine": "Quarantine",
    "universe": "Universe:",
    "window": "Window:",
    "gaps": "Gaps:",
    "requested": "Requested:",
    "fetched": "Fetched:",
    "written": "Written:",
    "skipped": "Skipped:",
    "failed": "Failed:",
}
CONNECTED_MESSAGES = {"paper": "connected: paper (dry-run)", "dhan": "connected: dhan"}
ENV_FILE_LABEL = "env_file="
HEALTH_OUTPUT_LABELS = {"session": "session=", "environment": "environment="}
NO_RUNTIME_BOUND_MESSAGE = "no runtime bound"
NO_QUOTE_FOR_MESSAGE = "no quote for"
QUOTE_FAILED_MESSAGE = "quote failed"
ORDER_FAILED_MESSAGE = "order failed"
WATCH_OUTPUT_LABELS = {
    "watching": "Watching",
    "ltp": "LTP:",
    "no_quote": "No quote available",
    "error": "Error:",
}
NO_POSITIONS_MESSAGE = "No positions"
NO_ORDERS_MESSAGE = "No orders"
ACCOUNT_OUTPUT_LABELS = {"account": "Account:", "balance": "Balance:"}
POSITIONS_HEADER = ["Symbol", "Qty", "Avg Price", "LTP", "P&L"]
ORDERS_HEADER = ["Order ID", "Symbol", "Side", "Status", "Qty"]
N_A_PLACEHOLDER = "N/A"
SERVE_FAILED_MESSAGE = "serve failed"
WORKERS_MIN_MESSAGE = "workers must be >= 1"

WS_MESSAGE_TYPE_KEY = "type"
WS_INSTRUMENT_KEY = "instrument"
WS_LTP_KEY = "ltp"
WS_BID_KEY = "bid"
WS_ASK_KEY = "ask"
WS_BIDS_KEY = "bids"
WS_ASKS_KEY = "asks"
WS_PRICE_KEY = "price"
WS_QUANTITY_KEY = "quantity"
WS_ORDER_ID_KEY = "order_id"
WS_STATUS_KEY = "status"
WS_SIDE_KEY = "side"
WS_AVG_PRICE_KEY = "avg_price"
WS_REALIZED_PNL_KEY = "realized_pnl"
WS_MARK_PRICE_KEY = "mark_price"
WS_MARKED_AT_KEY = "marked_at"
WS_MARK_SOURCE_KEY = "mark_source"
WS_OPEN_KEY = "open"
WS_HIGH_KEY = "high"
WS_LOW_KEY = "low"
WS_CLOSE_KEY = "close"
WS_VOLUME_KEY = "volume"
WS_CLOSED_KEY = "closed"
WS_RUN_ID_KEY = "run_id"
WS_PROVISIONAL_KEY = "provisional"
WS_TIME_KEY = "time"
WS_DROPPED_KEY = "dropped"
WS_QUEUED_TICKS_KEY = "queued_ticks"
WS_QUEUED_CONTROL_KEY = "queued_control"
WS_DEPTH_LEVELS_KEY = "depth_levels"
WS_MAX_STREAM_INSTRUMENTS_KEY = "max_stream_instruments"
WS_FEED_STATE_KEY = "feed_state"
WS_FEED_READY_KEY = "feed_ready"
#: The top-level ``feed_status`` frame and the nested ``feed`` object on the
#: ``subscribed`` ack are two different shapes and are not interchangeable: the
#: frame carries ``state``/``ready``, the nested object carries
#: ``feed_state``/``feed_ready``. These two name the frame's keys, which had been
#: spelled as the nested pair's constants — a substitution that reads as a
#: harmless de-literalisation and instead renames the wire field, breaking every
#: reader of ``feed_status_payload()["state"]``.
WS_FEED_STATUS_STATE_KEY = "state"
WS_FEED_STATUS_READY_KEY = "ready"
WS_INSTRUMENTS_KEY = "instruments"
WS_DEPTH_KEY = "depth"
WS_MESSAGE_KEY = "message"
WS_TOTAL_BARS_KEY = "total_bars"
WS_SIMULATED_KEY = "simulated"
WS_WALL_SECONDS_PER_BAR_KEY = "wall_seconds_per_bar"
WS_SPEED_KEY = "speed"
WS_BAR_INDEX_KEY = "bar_index"
WS_INDEX_KEY = "index"
WS_TARGET_INDEX_KEY = "target_index"
WS_START_TIME_KEY = "start_time"
WS_SEED_KEY = "seed"
WS_TICKS_PER_BAR_KEY = "ticks_per_bar"
WS_MINUTES_KEY = "minutes"
WS_METHOD_KEY = "method"
WS_PAUSED_KEY = "paused"
WS_SNAPSHOT_KEY = "snapshot"
WS_BARS_KEY = "bars"
WS_INDICATORS_KEY = "indicators"
WS_IDS_KEY = "ids"
WS_MODE_KEY = "mode"
WS_LAST_CLOSED_KEY = "lastClosed"
WS_CURRENT_BUCKET_KEY = "currentBucket"

WS_SUBSCRIBE_BARS = "subscribe_bars"
WS_INDICATOR_SUB = "indicator-sub"
WS_INDICATOR_UNSUB = "indicator-unsub"
WS_REPLAY_START = "replay_start"
WS_REPLAY_PAUSE = "replay_pause"
WS_REPLAY_SEEK = "replay_seek"

WORKSPACE_DATA_KEY = "data"
WORKSPACE_REVISION_KEY = "revision"
WORKSPACE_LAYOUT_ID_KEY = "layout_id"
WORKSPACE_UPDATED_AT_KEY = "updated_at"
WORKSPACE_FIELDS = {"layout_id", "data", "revision", "updated_at"}

AUTH_AUTHENTICATED_KEY = "authenticated"
AUTH_SESSION_AUTH_AVAILABLE_KEY = "session_auth_available"
AUTH_SUBJECT_KEY = "subject"
AUTH_CSRF_TOKEN_KEY = "csrf_token"
AUTH_SESSION_FIELDS = {"subject", "csrf_secret", "created_at", "last_seen"}
LOGIN_FIELDS = {"password"}
LOGOUT_FIELDS = {"logged_out"}
LOGGED_OUT_KEY = "logged_out"
PASSWORD_KEY = "password"
ORDER_ID_KEY = "order_id"
MODIFIED_KEY = "modified"
CANCELLED_KEY = "cancelled"

UNKNOWN_STRATEGY_KEY = "unknown strategy"
UNKNOWN_PARAMS_KEY = "unknown params"
STRATEGY_UNAVAILABLE_KEY = "strategy unavailable"
UNSUPPORTED_INTERVAL_KEY = "unsupported interval"
SYMBOL_REQUIRED_KEY = "'symbol' required"
NO_DATALAKE_DATA_KEY = "no datalake data for"
UNKNOWN_SCANNER_KEY = "unknown scanner"
WINDOW_DAYS_INTEGER_KEY = "window_days must be an integer"
WINDOW_DAYS_RANGE_KEY = "window_days must be 5..120"
BODY_ID_REQUIRED_KEY = "body must include string 'id'"
PARAMS_OBJECT_KEY = "'params' must be an object"
TRANSFORM_BARS_REQUIRED_KEY = "transform requires a non-empty bars array"
PROFILE_BARS_REQUIRED_KEY = "profile requires a non-empty bars array"
BODY_OBJECT_KEY = "body must be an object"
FROM_UTC_SECONDS_KEY = "'from' must be UTC seconds"
TO_UTC_SECONDS_KEY = "'to' must be UTC seconds"
ORDERBOOK_UNAVAILABLE_KEY = "orderbook unavailable"
POSITIONS_UNAVAILABLE_KEY = "positions unavailable"
MALFORMED_ORDER_ROW_KEY = "malformed order row"
ACCOUNT_UNAVAILABLE_KEY = "account unavailable"
NO_WORKSPACE_KEY = "no workspace"
STALE_REVISION_KEY = "stale revision"
LAYOUT_ID_LENGTH_KEY = "layout_id must be 1..256 chars"
INSTRUMENTS_NON_EMPTY_KEY = "instruments must be a non-empty list"
INVALID_JSON_KEY = "invalid JSON"
MESSAGE_OBJECT_KEY = "message must be an object"
UNKNOWN_MESSAGE_TYPE_KEY = "unknown message type"
SUBSCRIBE_BARS_FORMAT_KEY = "subscribe_bars needs 'bars': [{instrument, interval}]"
BAD_BAR_SUBSCRIPTION_KEY = "bad bar subscription"
INDICATOR_SUB_FORMAT_KEY = "indicator-sub needs 'indicators': [{instrument, interval, ids, mode?}]"
INDICATOR_UNSUB_FORMAT_KEY = "indicator-unsub needs 'indicators': [{instrument, interval, ids?}]"
REPLAY_START_INSTRUMENT_KEY = "replay_start needs 'instrument'"
REPLAY_INSTRUMENT_FORMAT_KEY = "replay instrument must be EXCHANGE:SYMBOL"
UNSUPPORTED_REPLAY_INTERVAL_KEY = "unsupported replay interval"
REPLAY_METHOD_KEY = "method must be ohlc, anchored, or bridge"
REPLAY_DATASET_EMPTY_KEY = "replay dataset is empty"
REPLAY_QUOTE_TIMESTAMP_KEY = "replay quote has no timestamp"
REPLAY_SEEK_ACTIVE_KEY = "replay seek needs an active replay"
REPLAY_SEEK_RESTART_FAILED_KEY = "replay seek restart failed"
ORDER_STREAM_UNAVAILABLE_KEY = "order stream unavailable"
CONTROL_QUEUE_OVERFLOW_CLOSE_REASON_KEY = "control queue overflow; reconnect to resync"
INVALID_API_KEY_CLOSE_REASON_KEY = "invalid api key"
AUTHENTICATION_REQUIRED_CLOSE_REASON_KEY = "authentication required"
SESSION_AUTH_NOT_AVAILABLE_KEY = "Session auth not available"
NO_LOGIN_CREDENTIALS_KEY = "No login credentials configured"
INVALID_CREDENTIALS_KEY = "Invalid credentials"
INVALID_JSON_BODY_KEY = "Invalid JSON body"
RATE_LIMIT_EXCEEDED_KEY = "rate limit exceeded; too many order mutations"
CSRF_ORIGIN_MISMATCH_KEY = "CSRF: origin mismatch"
CSRF_REFERER_MISMATCH_KEY = "CSRF: referer mismatch"
CSRF_TOKEN_INVALID_KEY = "CSRF token invalid or missing"
INVALID_API_KEY_KEY = "Invalid API key"
INVALID_API_KEY_MESSAGE = INVALID_API_KEY_KEY
ERROR_CODES = {
    "bad_request": 400,
    "unauthorized": 401,
    "not_found": 404,
    "conflict": 409,
    "validation": 422,
    "rate_limited": 429,
    "internal_error": 500,
    "upstream_unavailable": 502,
    "no_session": 401,
}
ERROR_ENVELOPE_KEY = "error"
ERROR_DETAIL_KEYS_KEY = {"code": "code", "message": "message"}
ERROR_DETAIL_KEYS = ERROR_DETAIL_KEYS_KEY

UNKNOWN_STRATEGY_MESSAGE = UNKNOWN_STRATEGY_KEY
UNKNOWN_PARAMS_MESSAGE = UNKNOWN_PARAMS_KEY
STRATEGY_UNAVAILABLE_MESSAGE = STRATEGY_UNAVAILABLE_KEY
UNSUPPORTED_INTERVAL_MESSAGE = UNSUPPORTED_INTERVAL_KEY
SYMBOL_REQUIRED_MESSAGE = SYMBOL_REQUIRED_KEY
NO_DATALAKE_DATA_MESSAGE = NO_DATALAKE_DATA_KEY
UNKNOWN_SCANNER_MESSAGE = UNKNOWN_SCANNER_KEY
WINDOW_DAYS_INTEGER_MESSAGE = WINDOW_DAYS_INTEGER_KEY
WINDOW_DAYS_RANGE_MESSAGE = WINDOW_DAYS_RANGE_KEY
BODY_ID_REQUIRED_MESSAGE = BODY_ID_REQUIRED_KEY
PARAMS_OBJECT_MESSAGE = PARAMS_OBJECT_KEY
TRANSFORM_BARS_REQUIRED_MESSAGE = TRANSFORM_BARS_REQUIRED_KEY
PROFILE_BARS_REQUIRED_MESSAGE = PROFILE_BARS_REQUIRED_KEY
BODY_OBJECT_MESSAGE = BODY_OBJECT_KEY
FROM_UTC_SECONDS_MESSAGE = FROM_UTC_SECONDS_KEY
TO_UTC_SECONDS_MESSAGE = TO_UTC_SECONDS_KEY
ORDERBOOK_UNAVAILABLE_MESSAGE = ORDERBOOK_UNAVAILABLE_KEY
POSITIONS_UNAVAILABLE_MESSAGE = POSITIONS_UNAVAILABLE_KEY
MALFORMED_ORDER_ROW_MESSAGE = MALFORMED_ORDER_ROW_KEY
ACCOUNT_UNAVAILABLE_MESSAGE = ACCOUNT_UNAVAILABLE_KEY
NO_WORKSPACE_MESSAGE = NO_WORKSPACE_KEY
STALE_REVISION_MESSAGE = STALE_REVISION_KEY
LAYOUT_ID_LENGTH_MESSAGE = LAYOUT_ID_LENGTH_KEY
INSTRUMENTS_NON_EMPTY_MESSAGE = INSTRUMENTS_NON_EMPTY_KEY
INVALID_JSON_MESSAGE = INVALID_JSON_KEY
MESSAGE_OBJECT_MESSAGE = MESSAGE_OBJECT_KEY
UNKNOWN_MESSAGE_TYPE_MESSAGE = UNKNOWN_MESSAGE_TYPE_KEY
SUBSCRIBE_BARS_FORMAT_MESSAGE = SUBSCRIBE_BARS_FORMAT_KEY
BAD_BAR_SUBSCRIPTION_MESSAGE = BAD_BAR_SUBSCRIPTION_KEY
INDICATOR_SUB_FORMAT_MESSAGE = INDICATOR_SUB_FORMAT_KEY
INDICATOR_UNSUB_FORMAT_MESSAGE = INDICATOR_UNSUB_FORMAT_KEY
REPLAY_START_INSTRUMENT_MESSAGE = REPLAY_START_INSTRUMENT_KEY
REPLAY_INSTRUMENT_FORMAT_MESSAGE = REPLAY_INSTRUMENT_FORMAT_KEY
UNSUPPORTED_REPLAY_INTERVAL_MESSAGE = UNSUPPORTED_REPLAY_INTERVAL_KEY
REPLAY_METHOD_MESSAGE = REPLAY_METHOD_KEY
REPLAY_DATASET_EMPTY_MESSAGE = REPLAY_DATASET_EMPTY_KEY
REPLAY_QUOTE_TIMESTAMP_MESSAGE = REPLAY_QUOTE_TIMESTAMP_KEY
REPLAY_SEEK_ACTIVE_MESSAGE = REPLAY_SEEK_ACTIVE_KEY
REPLAY_SEEK_RESTART_FAILED_MESSAGE = REPLAY_SEEK_RESTART_FAILED_KEY
ORDER_STREAM_UNAVAILABLE_MESSAGE = ORDER_STREAM_UNAVAILABLE_KEY
CONTROL_QUEUE_OVERFLOW_CLOSE_REASON = CONTROL_QUEUE_OVERFLOW_CLOSE_REASON_KEY
INVALID_API_KEY_CLOSE_REASON = INVALID_API_KEY_CLOSE_REASON_KEY
AUTHENTICATION_REQUIRED_CLOSE_REASON = AUTHENTICATION_REQUIRED_CLOSE_REASON_KEY
SESSION_AUTH_NOT_AVAILABLE_MESSAGE = SESSION_AUTH_NOT_AVAILABLE_KEY
NO_LOGIN_CREDENTIALS_MESSAGE = NO_LOGIN_CREDENTIALS_KEY
INVALID_CREDENTIALS_MESSAGE = INVALID_CREDENTIALS_KEY
INVALID_JSON_BODY_MESSAGE = INVALID_JSON_BODY_KEY
RATE_LIMIT_EXCEEDED_MESSAGE = RATE_LIMIT_EXCEEDED_KEY
CSRF_ORIGIN_MISMATCH_MESSAGE = CSRF_ORIGIN_MISMATCH_KEY
CSRF_REFERER_MISMATCH_MESSAGE = CSRF_REFERER_MISMATCH_KEY
CSRF_TOKEN_INVALID_MESSAGE = CSRF_TOKEN_INVALID_KEY

BROKER_UNAVAILABLE_MESSAGE = "broker unavailable"
ORDER_NOT_FOUND_MESSAGE = "order not found"
UNKNOWN_SYMBOL_MESSAGE = "unknown symbol"
UNKNOWN_PRODUCT_MESSAGE = "unknown product"
INVALID_PRICE_MESSAGE = "invalid price"
INSTRUMENT_ID_FORMAT_MESSAGE = "instrument_id must be in 'EXCHANGE:SYMBOL' format"
INSTRUMENT_ID_OR_EXCHANGE_SYMBOL_MESSAGE = (
    "provide either 'instrument_id' or both 'exchange' and 'symbol'"
)
MISSING_REQUIRED_FIELD_MESSAGE = "missing required field"
IDEMPOTENCY_REPLAY_MESSAGE = "idempotency_replay"
IDEMPOTENCY_KEY_REQUIRED_MESSAGE = "Idempotency-Key header is required for order mutations"
IDEMPOTENCY_KEY_TOO_LONG_MESSAGE = "Idempotency-Key is too long"
ORDERS_DISABLED_DURING_REPLAY_MESSAGE = "orders are disabled during replay"

TRADE_FIELDS = {"record": "record", "rejected": "rejected", "stop": "stop", "target": "target"}
METRIC_KEYS = {
    "total_return": "total_return",
    "sharpe": "sharpe",
    "max_drawdown": "max_drawdown",
    "num_trades": "num_trades",
    "num_rejected": "num_rejected",
    "num_unfilled": "num_unfilled",
    "total_fees": "total_fees",
}
EQUITY_CURVE_KEY = "equity_curve"
TRADES_KEY = "trades"
METRICS_KEY = "metrics"
MESSAGE_KEY = "message"
TABLE_KEY = "table"
META_KEY = "meta"
ID_KEY = "id"
POINTS_KEY = "points"
BARS_KEY = "bars"
SOURCE_KEY = "source"
TIMEFRAME_RESAMPLED_KEY = "timeframe_resampled"
LAST_CLOSED_TIME_KEY = "last_closed_time"
SYMBOL_KEY = "symbol"
EXCHANGE_KEY = "exchange"
INTERVAL_KEY = "interval"
CACHED_KEY = "cached"
INDICATORS_KEY = "indicators"
CAPABILITIES_KEY = "capabilities"
CAPABILITY_NAMES = {
    "supports_super_order": "supports_super_order",
    "supports_forever_order": "supports_forever_order",
    "supports_slice_order": "supports_slice_order",
    "supports_edis": "supports_edis",
    "supports_kill_switch": "supports_kill_switch",
}
ACCOUNT_FIELDS = {"balance": "balance", "margin": "margin", "equity": "equity"}
HOLDINGS_FIELDS = {"instrument_id": "instrument_id", "quantity": "quantity"}
POSITION_FIELDS = {
    "avg_price": "avg_price",
    "realized_pnl": "realized_pnl",
    "unrealized_pnl": "unrealized_pnl",
}
PORTFOLIO_KEYS = {"positions": "positions", "account": "account"}
BOOK_KEYS = {"orders": "orders", "positions": "positions"}
ORDER_FIELDS = {
    "id": "id",
    "symbol": "symbol",
    "exchange": "exchange",
    "side": "side",
    "type": "type",
    "qty": "qty",
    "filledQty": "filledQty",
    "price": "price",
    "triggerPrice": "triggerPrice",
    "status": "status",
}
ORDER_RESPONSE_FIELDS = {"order_id", "status", "message"}
NET_QTY_KEY = "netQty"
AVG_PRICE_KEY = "avg_price"
ACCOUNT_ID_KEY = "account_id"
BALANCE_KEY = "balance"
MARGIN_KEY = "margin"
UNREALIZED_PNL_KEY = "unrealized_pnl"
STRATEGIES_KEY = "strategies"
SCANNERS_KEY = "scanners"
BACKTESTABLE_KEY = "backtestable"
STRATEGY_FIELDS = {"id": "id", "params": "params"}
PARAM_FIELDS = {"name": "name", "type": "type", "default": "default"}
SCANNER_FIELDS = {
    "universe_size": "universe_size",
    "conditions": "conditions",
    "rank_by": "rank_by",
    "limit": "limit",
}
SAFETY_KEYS = {
    "feed": "feed",
    "unknown_orders": "unknown_orders",
    "reconciliation": "reconciliation",
    "strategies": "strategies",
    "boot_refused": "boot_refused",
    "reconciliation_checked": "reconciliation_checked",
    "drift": "drift",
}
UNKNOWN_ORDER_FIELDS = {
    "order_id": "order_id",
    "instrument": "instrument",
    "symbol": "symbol",
    "side": "side",
    "quantity": "quantity",
    "correlation_id": "correlation_id",
    "age_seconds": "age_seconds",
    "since": "since",
}
RECONCILIATION_FIELDS = {
    "last_checked_at": "last_checked_at",
    "totals_by_severity": "totals_by_severity",
    "refusing_severities": "refusing_severities",
    "refusing": "refusing",
}
STRATEGY_SAFETY_FIELDS = {
    "strategy_id": "strategy_id",
    "version": "version",
    "lifecycle_state": "lifecycle_state",
    "promotion_status": "promotion_status",
    "report_id": "report_id",
    "promotion_failures": "promotion_failures",
}
FEED_STATUS_FIELDS = {
    "state": "state",
    "ready": "ready",
    "generation": "generation",
    "last_event_at": "last_event_at",
    "age_seconds": "age_seconds",
}
LIVE_ORDERS_ENABLED_KEY = "live_orders_enabled"

STRATEGY_NAMES = {
    "sma_cross": "sma_cross",
    "mean_reversion": "mean_reversion",
    "bracket_breakout": "bracket_breakout",
}
STRATEGY_PARAM_NAMES = {
    "fast": "fast",
    "slow": "slow",
    "period": "period",
    "lookback": "lookback",
    "atr_period": "atr_period",
    "stop_atr": "stop_atr",
    "reward": "reward",
}
STRATEGY_PARAM_TYPES = {"int": "int", "float": "float"}
STRATEGY_DEFAULTS = {
    "sma_cross": {"fast": 5, "slow": 20},
    "mean_reversion": {"period": 14, "lookback": 20},
    "bracket_breakout": {"atr_period": 14, "stop_atr": 1.5, "reward": 2.0},
}
STRATEGY_FACTORIES = {
    "sma_cross": "sma_cross.SmaCrossStrategy",
    "mean_reversion": "mean_reversion.MeanReversionStrategy",
    "bracket_breakout": "bracket_breakout.BracketBreakoutStrategy",
}
PROFILE_IDS = {"seasonality": "seasonality"}
SEASONALITY_PARAMS = {
    "startYear": "startYear",
    "ignoredMonths": "ignoredMonths",
    "cutoffPercent": "cutoffPercent",
    "tablePosition": "tablePosition",
    "tableWidth": "tableWidth",
    "tableHeight": "tableHeight",
    "showAvg": "showAvg",
    "showStDev": "showStDev",
    "showPos": "showPos",
}

PRODUCT_BY_WIRE = {"MIS": "MIS", "CNC": "CNC", "NRML": "NRML", "MTF": "MTF"}
PRODUCT_TYPES = {"INTRADAY": "INTRADAY", "DELIVERY": "DELIVERY", "MARGIN": "MARGIN", "MTF": "MTF"}
ORDER_TYPES = {"MARKET": "MARKET", "LIMIT": "LIMIT", "STOP": "STOP", "STOP_LIMIT": "STOP_LIMIT"}
ORDER_STATUSES = {
    "NEW": "NEW",
    "PENDING": "PENDING",
    "SUBMITTED": "SUBMITTED",
    "ACK": "ACK",
    "PARTIALLY_FILLED": "PARTIALLY_FILLED",
    "FILLED": "FILLED",
    "CANCELLED": "CANCELLED",
    "REJECTED": "REJECTED",
    "UNKNOWN": "UNKNOWN",
}
CHART_ORDER_STATUSES = {
    "NEW": "pending",
    "PENDING": "pending",
    "SUBMITTED": "working",
    "ACK": "working",
    "PARTIALLY_FILLED": "partial",
    "FILLED": "filled",
    "CANCELLED": "cancelled",
    "REJECTED": "rejected",
    "UNKNOWN": "unknown",
}
CHART_ORDER_TYPES = {"MARKET": "MARKET", "LIMIT": "LIMIT", "STOP": "SL", "STOP_LIMIT": "SL-M"}

UNSETTLED_ORDER_STATUSES = {"NEW", "PENDING", "ACK", "SUBMITTED", "PARTIALLY_FILLED", "UNKNOWN"}


__all__ = [
    "DEFAULT_INITIAL_CAPITAL",
    "DEFAULT_BPS_DIVISOR",
    "DEFAULT_FILL_DEDUP_LRU",
    "DEFAULT_TICK_SIZE",
    "DEFAULT_STOP_GAP_BPS",
    "DEFAULT_BACKTEST_RISK_FRACTION",
    "DEFAULT_WARMUP_BARS",
    "EMBARGO_FRACTION",
    "VALUE_AREA_PERCENT",
    "CCI_CONSTANT",
    "TRADING_DAYS_PER_YEAR",
    "EULER_MASCHERONI",
    "BB_EXPANSION_LENGTH",
    "ALMA_OFFSET",
    "ALMA_SIGMA",
    "T3_FACTOR",
    "DECISION_POINT",
    "BARS_PER_DAY",
    "CRYPTO_BARS_PER_DAY",
    "MAX_LOG_SIZE",
    "STOP_TIMEOUT",
    "SCHEDULER_INTERVAL",
    "RECONCILIATION_INTERVAL",
    "CADENCE_WINDOW",
    "ORDER_FEED_STALE_AFTER",
    "LEDGER_SIZE",
    "FORMING_FRAME_MIN_INTERVAL",
    "LATE_TICK_REVISION_SECONDS",
    "DEDUP_WINDOW",
    "MAX_TAPE_BYTES",
    "MASTER_TTL_HOURS",
    "MASTER_CLEANUP_DAYS",
    "REFRESH_BUFFER_SECONDS",
    "DHAN_REFRESH_BUFFER_MINUTES",
    "FETCH_TIMEOUT",
    "IMPERSONATE",
    "REDIRECT_URI",
    "PAPER_CASH",
    "PAPER_SPREAD_BPS",
    "PAPER_LEVEL_DEPTH",
    "PAPER_LEVELS",
    "PAPER_TICK_SIZE",
    "BACKTEST_SLIPPAGE_BPS",
    "DEFAULT_LOOKBACK",
    "CACHE_TTL_SECONDS",
    "TAIL_LIMIT",
    "TICK_THROTTLE_SECONDS",
    "POINTS_PER_PUSH",
    "TAIL_CACHE_MAX_ENTRIES",
    "TAIL_CACHE_MAX_AGE_SECONDS",
    "TAIL_LOAD_JOIN_SECONDS",
    "RATE_LIMIT_MAX",
    "RATE_LIMIT_WINDOW",
    "SESSION_ABSOLUTE_TTL",
    "SESSION_IDLE_TTL",
    "SESSION_ID_BYTES",
    "CSRF_PURPOSE",
    "OUTBOUND_QUEUE_MAX",
    "CONTROL_QUEUE_MAX",
    "WS_CLOSE_CONTROL_OVERFLOW",
    "WS_CLOSE_POLICY_VIOLATION",
    "WS_CLOSE_INTERNAL_ERROR",
    "FEED_STATUS_POLL_SECONDS",
    "HISTORY_CACHE_MAXSIZE",
    "LRU_CACHE_MAXSIZE",
    "TAIL_LOOKBACK_DAYS",
    "REPLAY_LOOKBACK_DAYS",
    "MIN_SPEED",
    "MAX_SPEED",
    "MIN_TICKS_PER_BAR",
    "MAX_TICKS_PER_BAR",
    "MIN_MINUTES",
    "MAX_MINUTES",
    "MAX_START_TIME",
    "SLEEP_INTERVAL",
    "DEFAULT_LIMIT",
    "MAX_LIMIT",
    "PAPER_SNAPSHOT_SPREAD",
    "PAPER_SNAPSHOT_LOOKBACK",
    "PAPER_LTP_CHECK",
    "DEFAULT_INTERVAL_STREAM",
    "DEFAULT_INTERVAL_CHART",
    "DEFAULT_UNIVERSE",
    "MAX_RESULTS_SMALL",
    "MAX_RESULTS_LARGE",
    "MAX_LIMIT_LARGE",
    "BACKTEST_LOOKBACK_DAYS",
    "DEFAULT_HOST",
    "DEFAULT_PORT",
    "DEFAULT_WORKERS",
    "DEFAULT_BATCH_SIZE",
    "DEFAULT_MIN_GAP_STAMPS",
    "DEFAULT_BACKOFF_BASE",
    "DEFAULT_BACKOFF_MAX",
    "DEFAULT_SYNC_START_DAYS",
    "BAR_FREQ_MAP",
    "VERSION_DEFAULT",
    "DEFAULT_WINDOW_DAYS",
    "MIN_WINDOW_DAYS",
    "MAX_WINDOW_DAYS",
    "ADOPTION_TOLERANCE",
    "FUNDS_DRIFT_THRESHOLD",
    "RECONCILIATION_TOLERANCE",
    "NO_SESSION_BOUND",
    "FEED_NOT_READY",
    "TAPE_RECORD_ENV_VAR",
    "DHAN_COOLDOWN_SECONDS",
    "UPSTOX_COOLDOWN_SECONDS",
    "MIN_MASTER_ROWS",
    "DHAN_EQUITY_UNIVERSE",
    "UPSTOX_EQUITY_UNIVERSE",
    "SEGMENT_CANONICAL_DHAN",
    "SEGMENT_CANONICAL_UPSTOX",
    "PAPER_DEFAULT",
    "MODES",
    "READY_STATE",
    "DEPTH_MODES",
    "RECOVERY_TRIGGERS",
    "BOOK_MATCHING",
    "SLIPPAGE_MODEL",
    "FILL_PATHS",
    "AVG_PRICE_FALLBACK_PREFIX",
    "BOOKED_FILL_REASON",
    "WINDOW_EXPIRED_REASON",
    "STALE_REASON",
    "LARGE_JUMP_TRIGGER",
    "MISSING_BAR_TRIGGER",
    "ORDERS_STREAM",
    "SYSTEM_STREAM",
    "UNKNOWN_STATUS",
    "HIGH_SEVERITY",
    "CRITICAL_SEVERITY",
    "LOW_SEVERITY",
    "DRIFT_KINDS",
    "QUANTITY_MISMATCH_REASON",
    "STATUS_REASON",
    "MISSING_REASON",
    "BALANCE_ATTRIBUTE",
    "EQUITY_ATTRIBUTE",
    "PASSED_STATUS",
    "NOT_PROMOTED_STATUS",
    "NOT_EVALUATED_STATUS",
    "PROMOTABLE_STATUS",
    "NOT_PROMOTABLE_STATUS",
    "UNREADABLE_POSITION_REASON",
    "UNREADABLE_QUANTITY_REASON",
    "UNREADABLE_INSTRUMENT_REASON",
    "UNSETTLED_FILL_REASON",
    "VENUE_OMITTED_REASON",
    "UNSETTLED_GUARD_UNAVAILABLE_REASON",
    "VENUE_READ_FAILED_REASON",
    "VENUE_READ_PARTIAL_REASON",
    "VENUE_READ_STALE_REASON",
    "VENUE_GET_POSITIONS_FAILED_REASON",
    "UNREADABLE_INSTRUMENT_PLACEHOLDER",
    "IDEMPOTENCY_REPLAY",
    "IDEMPOTENCY_IN_FLIGHT",
    "IDEMPOTENCY_KEY_HEADER",
    "SUBMITTED_STATUS",
    "MODIFIED_STATUS",
    "CANCELLED_STATUS",
    "SERVICE_NAME",
    "CLI_PROG_NAME",
    "CLI_DESCRIPTION",
    "FASTAPI_TITLE",
    "FASTAPI_VERSION",
    "SYNC_DHAN_ONLY_MESSAGE",
    "SYNC_OUTPUT_LABELS",
    "CONNECTED_MESSAGES",
    "ENV_FILE_LABEL",
    "HEALTH_OUTPUT_LABELS",
    "NO_RUNTIME_BOUND_MESSAGE",
    "NO_QUOTE_FOR_MESSAGE",
    "QUOTE_FAILED_MESSAGE",
    "ORDER_FAILED_MESSAGE",
    "WATCH_OUTPUT_LABELS",
    "NO_POSITIONS_MESSAGE",
    "NO_ORDERS_MESSAGE",
    "ACCOUNT_OUTPUT_LABELS",
    "POSITIONS_HEADER",
    "ORDERS_HEADER",
    "N_A_PLACEHOLDER",
    "SERVE_FAILED_MESSAGE",
    "WORKERS_MIN_MESSAGE",
    "WS_MESSAGE_TYPE_KEY",
    "WS_INSTRUMENT_KEY",
    "WS_LTP_KEY",
    "WS_BID_KEY",
    "WS_ASK_KEY",
    "WS_BIDS_KEY",
    "WS_ASKS_KEY",
    "WS_PRICE_KEY",
    "WS_QUANTITY_KEY",
    "WS_ORDER_ID_KEY",
    "WS_STATUS_KEY",
    "WS_SIDE_KEY",
    "WS_AVG_PRICE_KEY",
    "WS_REALIZED_PNL_KEY",
    "WS_MARK_PRICE_KEY",
    "WS_MARKED_AT_KEY",
    "WS_MARK_SOURCE_KEY",
    "WS_OPEN_KEY",
    "WS_HIGH_KEY",
    "WS_LOW_KEY",
    "WS_CLOSE_KEY",
    "WS_VOLUME_KEY",
    "WS_CLOSED_KEY",
    "WS_RUN_ID_KEY",
    "WS_PROVISIONAL_KEY",
    "WS_TIME_KEY",
    "WS_DROPPED_KEY",
    "WS_QUEUED_TICKS_KEY",
    "WS_QUEUED_CONTROL_KEY",
    "WS_DEPTH_LEVELS_KEY",
    "WS_MAX_STREAM_INSTRUMENTS_KEY",
    "WS_FEED_STATE_KEY",
    "WS_FEED_READY_KEY",
    "WS_FEED_STATUS_STATE_KEY",
    "WS_FEED_STATUS_READY_KEY",
    "WS_INSTRUMENTS_KEY",
    "WS_DEPTH_KEY",
    "WS_MESSAGE_KEY",
    "WS_TOTAL_BARS_KEY",
    "WS_SIMULATED_KEY",
    "WS_WALL_SECONDS_PER_BAR_KEY",
    "WS_SPEED_KEY",
    "WS_BAR_INDEX_KEY",
    "WS_INDEX_KEY",
    "WS_TARGET_INDEX_KEY",
    "WS_START_TIME_KEY",
    "WS_SEED_KEY",
    "WS_TICKS_PER_BAR_KEY",
    "WS_MINUTES_KEY",
    "WS_METHOD_KEY",
    "WS_PAUSED_KEY",
    "WS_SNAPSHOT_KEY",
    "WS_BARS_KEY",
    "WS_INDICATORS_KEY",
    "WS_IDS_KEY",
    "WS_MODE_KEY",
    "WS_LAST_CLOSED_KEY",
    "WS_CURRENT_BUCKET_KEY",
    "WS_SUBSCRIBE_BARS",
    "WS_INDICATOR_SUB",
    "WS_INDICATOR_UNSUB",
    "WS_REPLAY_START",
    "WS_REPLAY_PAUSE",
    "WS_REPLAY_SEEK",
    "WORKSPACE_DATA_KEY",
    "WORKSPACE_REVISION_KEY",
    "WORKSPACE_LAYOUT_ID_KEY",
    "WORKSPACE_UPDATED_AT_KEY",
    "WORKSPACE_FIELDS",
    "AUTH_AUTHENTICATED_KEY",
    "AUTH_SESSION_AUTH_AVAILABLE_KEY",
    "AUTH_SUBJECT_KEY",
    "AUTH_CSRF_TOKEN_KEY",
    "AUTH_SESSION_FIELDS",
    "LOGIN_FIELDS",
    "LOGOUT_FIELDS",
    "LOGGED_OUT_KEY",
    "PASSWORD_KEY",
    "ORDER_ID_KEY",
    "MODIFIED_KEY",
    "CANCELLED_KEY",
    "UNKNOWN_STRATEGY_KEY",
    "UNKNOWN_PARAMS_KEY",
    "STRATEGY_UNAVAILABLE_KEY",
    "UNSUPPORTED_INTERVAL_KEY",
    "SYMBOL_REQUIRED_KEY",
    "NO_DATALAKE_DATA_KEY",
    "UNKNOWN_SCANNER_KEY",
    "WINDOW_DAYS_INTEGER_KEY",
    "WINDOW_DAYS_RANGE_KEY",
    "BODY_ID_REQUIRED_KEY",
    "PARAMS_OBJECT_KEY",
    "TRANSFORM_BARS_REQUIRED_KEY",
    "PROFILE_BARS_REQUIRED_KEY",
    "BODY_OBJECT_KEY",
    "FROM_UTC_SECONDS_KEY",
    "TO_UTC_SECONDS_KEY",
    "ORDERBOOK_UNAVAILABLE_KEY",
    "POSITIONS_UNAVAILABLE_KEY",
    "MALFORMED_ORDER_ROW_KEY",
    "ACCOUNT_UNAVAILABLE_KEY",
    "NO_WORKSPACE_KEY",
    "STALE_REVISION_KEY",
    "LAYOUT_ID_LENGTH_KEY",
    "INSTRUMENTS_NON_EMPTY_KEY",
    "INVALID_JSON_KEY",
    "MESSAGE_OBJECT_KEY",
    "UNKNOWN_MESSAGE_TYPE_KEY",
    "SUBSCRIBE_BARS_FORMAT_KEY",
    "BAD_BAR_SUBSCRIPTION_KEY",
    "INDICATOR_SUB_FORMAT_KEY",
    "INDICATOR_UNSUB_FORMAT_KEY",
    "REPLAY_START_INSTRUMENT_KEY",
    "REPLAY_INSTRUMENT_FORMAT_KEY",
    "UNSUPPORTED_REPLAY_INTERVAL_KEY",
    "REPLAY_METHOD_KEY",
    "REPLAY_DATASET_EMPTY_KEY",
    "REPLAY_QUOTE_TIMESTAMP_KEY",
    "REPLAY_SEEK_ACTIVE_KEY",
    "REPLAY_SEEK_RESTART_FAILED_KEY",
    "ORDER_STREAM_UNAVAILABLE_KEY",
    "CONTROL_QUEUE_OVERFLOW_CLOSE_REASON_KEY",
    "INVALID_API_KEY_CLOSE_REASON_KEY",
    "AUTHENTICATION_REQUIRED_CLOSE_REASON_KEY",
    "SESSION_AUTH_NOT_AVAILABLE_KEY",
    "NO_LOGIN_CREDENTIALS_KEY",
    "INVALID_CREDENTIALS_KEY",
    "INVALID_JSON_BODY_KEY",
    "RATE_LIMIT_EXCEEDED_KEY",
    "CSRF_ORIGIN_MISMATCH_KEY",
    "CSRF_REFERER_MISMATCH_KEY",
    "CSRF_TOKEN_INVALID_KEY",
    "INVALID_API_KEY_KEY",
    "INVALID_API_KEY_MESSAGE",
    "ERROR_CODES",
    "ERROR_ENVELOPE_KEY",
    "ERROR_DETAIL_KEYS_KEY",
    "ERROR_DETAIL_KEYS",
    "UNKNOWN_STRATEGY_MESSAGE",
    "UNKNOWN_PARAMS_MESSAGE",
    "STRATEGY_UNAVAILABLE_MESSAGE",
    "UNSUPPORTED_INTERVAL_MESSAGE",
    "SYMBOL_REQUIRED_MESSAGE",
    "NO_DATALAKE_DATA_MESSAGE",
    "UNKNOWN_SCANNER_MESSAGE",
    "WINDOW_DAYS_INTEGER_MESSAGE",
    "WINDOW_DAYS_RANGE_MESSAGE",
    "BODY_ID_REQUIRED_MESSAGE",
    "PARAMS_OBJECT_MESSAGE",
    "TRANSFORM_BARS_REQUIRED_MESSAGE",
    "PROFILE_BARS_REQUIRED_MESSAGE",
    "BODY_OBJECT_MESSAGE",
    "FROM_UTC_SECONDS_MESSAGE",
    "TO_UTC_SECONDS_MESSAGE",
    "ORDERBOOK_UNAVAILABLE_MESSAGE",
    "POSITIONS_UNAVAILABLE_MESSAGE",
    "MALFORMED_ORDER_ROW_MESSAGE",
    "ACCOUNT_UNAVAILABLE_MESSAGE",
    "NO_WORKSPACE_MESSAGE",
    "STALE_REVISION_MESSAGE",
    "LAYOUT_ID_LENGTH_MESSAGE",
    "INSTRUMENTS_NON_EMPTY_MESSAGE",
    "INVALID_JSON_MESSAGE",
    "MESSAGE_OBJECT_MESSAGE",
    "UNKNOWN_MESSAGE_TYPE_MESSAGE",
    "SUBSCRIBE_BARS_FORMAT_MESSAGE",
    "BAD_BAR_SUBSCRIPTION_MESSAGE",
    "INDICATOR_SUB_FORMAT_MESSAGE",
    "INDICATOR_UNSUB_FORMAT_MESSAGE",
    "REPLAY_START_INSTRUMENT_MESSAGE",
    "REPLAY_INSTRUMENT_FORMAT_MESSAGE",
    "UNSUPPORTED_REPLAY_INTERVAL_MESSAGE",
    "REPLAY_METHOD_MESSAGE",
    "REPLAY_DATASET_EMPTY_MESSAGE",
    "REPLAY_QUOTE_TIMESTAMP_MESSAGE",
    "REPLAY_SEEK_ACTIVE_MESSAGE",
    "REPLAY_SEEK_RESTART_FAILED_MESSAGE",
    "ORDER_STREAM_UNAVAILABLE_MESSAGE",
    "CONTROL_QUEUE_OVERFLOW_CLOSE_REASON",
    "INVALID_API_KEY_CLOSE_REASON",
    "AUTHENTICATION_REQUIRED_CLOSE_REASON",
    "SESSION_AUTH_NOT_AVAILABLE_MESSAGE",
    "NO_LOGIN_CREDENTIALS_MESSAGE",
    "INVALID_CREDENTIALS_MESSAGE",
    "INVALID_JSON_BODY_MESSAGE",
    "RATE_LIMIT_EXCEEDED_MESSAGE",
    "CSRF_ORIGIN_MISMATCH_MESSAGE",
    "CSRF_REFERER_MISMATCH_MESSAGE",
    "CSRF_TOKEN_INVALID_MESSAGE",
    "BROKER_UNAVAILABLE_MESSAGE",
    "ORDER_NOT_FOUND_MESSAGE",
    "UNKNOWN_SYMBOL_MESSAGE",
    "UNKNOWN_PRODUCT_MESSAGE",
    "INVALID_PRICE_MESSAGE",
    "INSTRUMENT_ID_FORMAT_MESSAGE",
    "INSTRUMENT_ID_OR_EXCHANGE_SYMBOL_MESSAGE",
    "MISSING_REQUIRED_FIELD_MESSAGE",
    "IDEMPOTENCY_REPLAY_MESSAGE",
    "IDEMPOTENCY_KEY_REQUIRED_MESSAGE",
    "IDEMPOTENCY_KEY_TOO_LONG_MESSAGE",
    "ORDERS_DISABLED_DURING_REPLAY_MESSAGE",
    "TRADE_FIELDS",
    "METRIC_KEYS",
    "EQUITY_CURVE_KEY",
    "TRADES_KEY",
    "METRICS_KEY",
    "MESSAGE_KEY",
    "TABLE_KEY",
    "META_KEY",
    "ID_KEY",
    "POINTS_KEY",
    "BARS_KEY",
    "SOURCE_KEY",
    "TIMEFRAME_RESAMPLED_KEY",
    "LAST_CLOSED_TIME_KEY",
    "SYMBOL_KEY",
    "EXCHANGE_KEY",
    "INTERVAL_KEY",
    "CACHED_KEY",
    "INDICATORS_KEY",
    "CAPABILITIES_KEY",
    "CAPABILITY_NAMES",
    "ACCOUNT_FIELDS",
    "HOLDINGS_FIELDS",
    "POSITION_FIELDS",
    "PORTFOLIO_KEYS",
    "BOOK_KEYS",
    "ORDER_FIELDS",
    "ORDER_RESPONSE_FIELDS",
    "NET_QTY_KEY",
    "AVG_PRICE_KEY",
    "ACCOUNT_ID_KEY",
    "BALANCE_KEY",
    "MARGIN_KEY",
    "UNREALIZED_PNL_KEY",
    "STRATEGIES_KEY",
    "SCANNERS_KEY",
    "BACKTESTABLE_KEY",
    "STRATEGY_FIELDS",
    "PARAM_FIELDS",
    "SCANNER_FIELDS",
    "SAFETY_KEYS",
    "UNKNOWN_ORDER_FIELDS",
    "RECONCILIATION_FIELDS",
    "STRATEGY_SAFETY_FIELDS",
    "FEED_STATUS_FIELDS",
    "LIVE_ORDERS_ENABLED_KEY",
    "STRATEGY_NAMES",
    "STRATEGY_PARAM_NAMES",
    "STRATEGY_PARAM_TYPES",
    "STRATEGY_DEFAULTS",
    "STRATEGY_FACTORIES",
    "PROFILE_IDS",
    "SEASONALITY_PARAMS",
    "PRODUCT_BY_WIRE",
    "PRODUCT_TYPES",
    "ORDER_TYPES",
    "ORDER_STATUSES",
    "CHART_ORDER_STATUSES",
    "CHART_ORDER_TYPES",
    "UNSETTLED_ORDER_STATUSES",
]
