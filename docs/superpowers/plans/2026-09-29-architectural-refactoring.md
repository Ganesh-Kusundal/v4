# Architectural Refactoring Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Eliminate 300+ shotgun surgery code smells across 16 packages by centralizing constants, unifying duplicated logic, enforcing boundary rules, and establishing coding standards.

**Architecture:** 5-wave parallel execution. Wave 0 creates the foundation (constants module). Waves 1-4 run in parallel across independent subsystems. Each task follows TDD: write failing test, implement, verify, commit, review.

**Tech Stack:** Python 3.13, pytest, mypy, import-linter, tradex_domain

## Global Constraints

1. **All price values must use `Decimal` from `domain/value_objects.py`, never `float`.**
2. **All timestamps must be converted to IST via `to_ist_naive()` from `domain/market_calendar.py`, never inline.**
3. **All symbol normalization must use `normalize_symbol()` from `domain/value_objects.py`, never `strip().upper()`.**
4. **All order construction must use `create_order()` from `domain/order_factory.py`, never direct `Order()` calls.**
5. **All fill fingerprinting must use `fingerprint()` from `domain/fill_fingerprint.py`, never inline tuples.**
6. **All mark-to-market must use `select_mark()` from `domain/marketing.py`, never inline bid/ask selection.**
7. **All indicator math must use shared helpers from `analytics/_math.py`, never re-implemented.**
8. **All metric names must be imported from `observability/metrics.py`, never hardcoded strings.**
9. **All error messages must be defined in `domain/constants.py`, never hardcoded strings.**
10. **All WebSocket message types must be defined in `domain/enums.py`, never hardcoded strings.**
11. **No package may reach through an abstraction to its internals (Law of Demeter).**
12. **All domain objects must use `@dataclass(frozen=True, slots=True)`.**
13. **All public modules must declare `__all__`.**
14. **All public functions must have complete type annotations.**

---

## Wave 0: Foundation (Sequential — must complete before any other wave)

### Task 0.1: Create `domain/constants.py` — Central Constants Module

**Files:**
- Create: `domain/src/tradex_domain/constants.py`
- Modify: All files that currently hardcode shared literals (300+ files)
- Test: `tests/domain/test_constants.py`

**Interfaces:**
- Consumes: Nothing (foundation)
- Produces: `SESSION_OPEN`, `SESSION_CLOSE`, `IST_TIMEZONE`, `DEFAULT_INITIAL_CAPITAL`, `DEFAULT_BPS_DIVISOR`, `DEFAULT_FILL_DEDUP_LRU`, `DEFAULT_TICK_SIZE`, `DEFAULT_STOP_GAP_BPS`, `DEFAULT_BACKTEST_RISK_FRACTION`, `DEFAULT_WARMUP_BARS`, `EMBARGO_FRACTION`, `VALUE_AREA_PERCENT`, `CCI_CONSTANT`, `TRADING_DAYS_PER_YEAR`, `EULER_MASCHERONI`, `BB_EXPANSION_LENGTH`, `ALMA_OFFSET`, `ALMA_SIGMA`, `T3_FACTOR`, `DECISION_POINT`, `BARS_PER_DAY`, `CRYPTO_BARS_PER_DAY`, `MAX_LOG_SIZE`, `STOP_TIMEOUT`, `SCHEDULER_INTERVAL`, `RECONCILIATION_INTERVAL`, `CADENCE_WINDOW`, `ORDER_FEED_STALE_AFTER`, `LEDGER_SIZE`, `FORMING_FRAME_MIN_INTERVAL`, `LATE_TICK_REVISION_SECONDS`, `DEDUP_WINDOW`, `MAX_TAPE_BYTES`, `MASTER_TTL_HOURS`, `MASTER_CLEANUP_DAYS`, `REFRESH_BUFFER_SECONDS`, `DHAN_REFRESH_BUFFER_MINUTES`, `FETCH_TIMEOUT`, `IMPERSONATE`, `REDIRECT_URI`, `PAPER_CASH`, `PAPER_SPREAD_BPS`, `PAPER_LEVEL_DEPTH`, `PAPER_LEVELS`, `PAPER_TICK_SIZE`, `BACKTEST_SLIPPAGE_BPS`, `DEFAULT_LOOKBACK`, `CACHE_TTL_SECONDS`, `TAIL_LIMIT`, `TICK_THROTTLE_SECONDS`, `POINTS_PER_PUSH`, `TAIL_CACHE_MAX_ENTRIES`, `TAIL_CACHE_MAX_AGE_SECONDS`, `TAIL_LOAD_JOIN_SECONDS`, `RATE_LIMIT_MAX`, `RATE_LIMIT_WINDOW`, `SESSION_ABSOLUTE_TTL`, `SESSION_IDLE_TTL`, `SESSION_ID_BYTES`, `CSRF_PURPOSE`, `OUTBOUND_QUEUE_MAX`, `CONTROL_QUEUE_MAX`, `WS_CLOSE_CONTROL_OVERFLOW`, `WS_CLOSE_POLICY_VIOLATION`, `WS_CLOSE_INTERNAL_ERROR`, `FEED_STATUS_POLL_SECONDS`, `HISTORY_CACHE_MAXSIZE`, `LRU_CACHE_MAXSIZE`, `TAIL_LOOKBACK_DAYS`, `REPLAY_LOOKBACK_DAYS`, `MIN_SPEED`, `MAX_SPEED`, `MIN_TICKS_PER_BAR`, `MAX_TICKS_PER_BAR`, `MIN_MINUTES`, `MAX_MINUTES`, `MAX_START_TIME`, `SLEEP_INTERVAL`, `DEFAULT_LIMIT`, `MAX_LIMIT`, `PAPER_SNAPSHOT_SPREAD`, `PAPER_SNAPSHOT_LOOKBACK`, `PAPER_LTP_CHECK`, `DEFAULT_INTERVAL_STREAM`, `DEFAULT_INTERVAL_CHART`, `DEFAULT_UNIVERSE`, `MAX_RESULTS_SMALL`, `MAX_RESULTS_LARGE`, `MAX_LIMIT_LARGE`, `BACKTEST_LOOKBACK_DAYS`, `DEFAULT_HOST`, `DEFAULT_PORT`, `DEFAULT_WORKERS`, `DEFAULT_BATCH_SIZE`, `DEFAULT_MIN_GAP_STAMPS`, `DEFAULT_BACKOFF_BASE`, `DEFAULT_BACKOFF_MAX`, `DEFAULT_SYNC_START_DAYS`, `BAR_FREQ_MAP`, `VERSION_DEFAULT`, `DEFAULT_WINDOW_DAYS`, `MIN_WINDOW_DAYS`, `MAX_WINDOW_DAYS`, `ADOPTION_TOLERANCE`, `FUNDS_DRIFT_THRESHOLD`, `RECONCILIATION_TOLERANCE`, `NO_SESSION_BOUND`, `FEED_NOT_READY`, `TAPE_RECORD_ENV_VAR`, `DHAN_COOLDOWN_SECONDS`, `UPSTOX_COOLDOWN_SECONDS`, `DHAN_INTRADAY_MAX_DAYS`, `UPSTOX_MINUTE_MAX_DAYS`, `MIN_MASTER_ROWS`, `DHAN_EQUITY_UNIVERSE`, `UPSTOX_EQUITY_UNIVERSE`, `SEGMENT_CANONICAL_DHAN`, `SEGMENT_CANONICAL_UPSTOX`, `_SAFE_METHODS`, `_SUCCESS_STATUS`, `PAPER_DEFAULT`, `MODES`, `READY_STATE`, `DEPTH_MODES`, `RECOVERY_TRIGGERS`, `BOOK_MATCHING`, `SLIPPAGE_MODEL`, `FILL_PATHS`, `AVG_PRICE_FALLBACK_PREFIX`, `BOOKED_FILL_REASON`, `WINDOW_EXPIRED_REASON`, `STALE_REASON`, `LARGE_JUMP_TRIGGER`, `MISSING_BAR_TRIGGER`, `ORDERS_STREAM`, `SYSTEM_STREAM`, `UNKNOWN_STATUS`, `HIGH_SEVERITY`, `CRITICAL_SEVERITY`, `LOW_SEVERITY`, `DRIFT_KINDS`, `QUANTITY_MISMATCH_REASON`, `STATUS_REASON`, `MISSING_REASON`, `BALANCE_ATTRIBUTE`, `EQUITY_ATTRIBUTE`, `PASSED_STATUS`, `NOT_PROMOTED_STATUS`, `NOT_EVALUATED_STATUS`, `PROMOTABLE_STATUS`, `NOT_PROMOTABLE_STATUS`, `UNREADABLE_POSITION_REASON`, `UNREADABLE_QUANTITY_REASON`, `UNREADABLE_INSTRUMENT_REASON`, `UNSETTLED_FILL_REASON`, `VENUE_OMITTED_REASON`, `UNSETTLED_GUARD_UNAVAILABLE_REASON`, `UNSETTLED_ORDER_STATUSES`, `VENUE_READ_FAILED_REASON`, `VENUE_READ_PARTIAL_REASON`, `VENUE_READ_STALE_REASON`, `VENUE_GET_POSITIONS_FAILED_REASON`, `UNREADABLE_INSTRUMENT_PLACEHOLDER`, `IDEMPOTENCY_REPLAY`, `IDEMPOTENCY_IN_FLIGHT`, `IDEMPOTENCY_KEY_HEADER`, `PRODUCT_BY_WIRE`, `PRODUCT_TYPES`, `ORDER_TYPES`, `ORDER_STATUSES`, `CHART_ORDER_STATUSES`, `CHART_ORDER_TYPES`, `STRATEGY_NAMES`, `STRATEGY_PARAM_NAMES`, `STRATEGY_PARAM_TYPES`, `STRATEGY_DEFAULTS`, `STRATEGY_FACTORIES`, `PROFILE_IDS`, `SEASONALITY_PARAMS`, `WORKSPACE_FIELDS`, `AUTH_SESSION_FIELDS`, `LOGOUT_FIELDS`, `LOGIN_FIELDS`, `ORDER_RESPONSE_FIELDS`, `MODIFIED_STATUS`, `CANCELLED_STATUS`, `SUBMITTED_STATUS`, `IDEMPOTENCY_REPLAY_MESSAGE`, `IDEMPOTENCY_KEY_REQUIRED_MESSAGE`, `IDEMPOTENCY_KEY_TOO_LONG_MESSAGE`, `ORDERS_DISABLED_DURING_REPLAY_MESSAGE`, `BROKER_UNAVAILABLE_MESSAGE`, `ORDER_NOT_FOUND_MESSAGE`, `UNKNOWN_SYMBOL_MESSAGE`, `MISSING_REQUIRED_FIELD_MESSAGE`, `UNKNOWN_PRODUCT_MESSAGE`, `INSTRUMENT_ID_FORMAT_MESSAGE`, `INSTRUMENT_ID_OR_EXCHANGE_SYMBOL_MESSAGE`, `INVALID_PRICE_MESSAGE`, `UNKNOWN_STRATEGY_MESSAGE`, `UNKNOWN_PARAMS_MESSAGE`, `STRATEGY_UNAVAILABLE_MESSAGE`, `UNSUPPORTED_INTERVAL_MESSAGE`, `SYMBOL_REQUIRED_MESSAGE`, `NO_DATALAKE_DATA_MESSAGE`, `UNKNOWN_SCANNER_MESSAGE`, `WINDOW_DAYS_INTEGER_MESSAGE`, `WINDOW_DAYS_RANGE_MESSAGE`, `BODY_ID_REQUIRED_MESSAGE`, `PARAMS_OBJECT_MESSAGE`, `TRANSFORM_BARS_REQUIRED_MESSAGE`, `PROFILE_BARS_REQUIRED_MESSAGE`, `BODY_OBJECT_MESSAGE`, `FROM_UTC_SECONDS_MESSAGE`, `TO_UTC_SECONDS_MESSAGE`, `ORDERBOOK_UNAVAILABLE_MESSAGE`, `POSITIONS_UNAVAILABLE_MESSAGE`, `MALFORMED_ORDER_ROW_MESSAGE`, `ACCOUNT_UNAVAILABLE_MESSAGE`, `NO_WORKSPACE_MESSAGE`, `STALE_REVISION_MESSAGE`, `LAYOUT_ID_LENGTH_MESSAGE`, `INSTRUMENTS_NON_EMPTY_MESSAGE`, `INVALID_JSON_MESSAGE`, `MESSAGE_OBJECT_MESSAGE`, `UNKNOWN_MESSAGE_TYPE_MESSAGE`, `SUBSCRIBE_BARS_FORMAT_MESSAGE`, `BAD_BAR_SUBSCRIPTION_MESSAGE`, `INDICATOR_SUB_FORMAT_MESSAGE`, `INDICATOR_UNSUB_FORMAT_MESSAGE`, `REPLAY_START_INSTRUMENT_MESSAGE`, `REPLAY_INSTRUMENT_FORMAT_MESSAGE`, `UNSUPPORTED_REPLAY_INTERVAL_MESSAGE`, `REPLAY_METHOD_MESSAGE`, `REPLAY_DATASET_EMPTY_MESSAGE`, `REPLAY_QUOTE_TIMESTAMP_MESSAGE`, `REPLAY_SEEK_ACTIVE_MESSAGE`, `REPLAY_SEEK_RESTART_FAILED_MESSAGE`, `ORDER_STREAM_UNAVAILABLE_MESSAGE`, `CONTROL_QUEUE_OVERFLOW_CLOSE_REASON`, `INVALID_API_KEY_CLOSE_REASON`, `AUTHENTICATION_REQUIRED_CLOSE_REASON`, `SESSION_AUTH_NOT_AVAILABLE_MESSAGE`, `NO_LOGIN_CREDENTIALS_MESSAGE`, `INVALID_CREDENTIALS_MESSAGE`, `INVALID_JSON_BODY_MESSAGE`, `RATE_LIMIT_EXCEEDED_MESSAGE`, `CSRF_ORIGIN_MISMATCH_MESSAGE`, `CSRF_REFERER_MISMATCH_MESSAGE`, `CSRF_TOKEN_INVALID_MESSAGE`, `INVALID_API_KEY_MESSAGE`, `ERROR_CODES`, `ERROR_ENVELOPE_KEY`, `ERROR_DETAIL_KEYS`, `SERVICE_NAME`, `CLI_PROG_NAME`, `CLI_DESCRIPTION`, `FASTAPI_TITLE`, `FASTAPI_VERSION`, `SYNC_DHAN_ONLY_MESSAGE`, `SYNC_OUTPUT_LABELS`, `CONNECTED_MESSAGES`, `ENV_FILE_LABEL`, `HEALTH_OUTPUT_LABELS`, `NO_RUNTIME_BOUND_MESSAGE`, `NO_QUOTE_FOR_MESSAGE`, `QUOTE_FAILED_MESSAGE`, `ORDER_FAILED_MESSAGE`, `WATCH_OUTPUT_LABELS`, `NO_POSITIONS_MESSAGE`, `NO_ORDERS_MESSAGE`, `ACCOUNT_OUTPUT_LABELS`, `POSITIONS_HEADER`, `ORDERS_HEADER`, `N_A_PLACEHOLDER`, `SERVE_FAILED_MESSAGE`, `WORKERS_MIN_MESSAGE`, `TRADE_FIELDS`, `METRIC_KEYS`, `EQUITY_CURVE_KEY`, `TRADES_KEY`, `METRICS_KEY`, `MESSAGE_KEY`, `TABLE_KEY`, `META_KEY`, `ID_KEY`, `POINTS_KEY`, `BARS_KEY`, `SOURCE_KEY`, `TIMEFRAME_RESAMPLED_KEY`, `LAST_CLOSED_TIME_KEY`, `SYMBOL_KEY`, `EXCHANGE_KEY`, `INTERVAL_KEY`, `CACHED_KEY`, `INDICATORS_KEY`, `CAPABILITIES_KEY`, `CAPABILITY_NAMES`, `ACCOUNT_FIELDS`, `HOLDINGS_FIELDS`, `POSITION_FIELDS`, `PORTFOLIO_KEYS`, `BOOK_KEYS`, `ORDER_FIELDS`, `NET_QTY_KEY`, `AVG_PRICE_KEY`, `ACCOUNT_ID_KEY`, `BALANCE_KEY`, `MARGIN_KEY`, `UNREALIZED_PNL_KEY`, `STRATEGIES_KEY`, `SCANNERS_KEY`, `BACKTESTABLE_KEY`, `STRATEGY_FIELDS`, `PARAM_FIELDS`, `SCANNER_FIELDS`, `SAFETY_KEYS`, `UNKNOWN_ORDER_FIELDS`, `RECONCILIATION_FIELDS`, `STRATEGY_SAFETY_FIELDS`, `FEED_STATUS_FIELDS`, `LIVE_ORDERS_ENABLED_KEY`, `WS_MESSAGE_TYPE_KEY`, `WS_INSTRUMENT_KEY`, `WS_LTP_KEY`, `WS_BID_KEY`, `WS_ASK_KEY`, `WS_BIDS_KEY`, `WS_ASKS_KEY`, `WS_PRICE_KEY`, `WS_QUANTITY_KEY`, `WS_ORDER_ID_KEY`, `WS_STATUS_KEY`, `WS_SIDE_KEY`, `WS_AVG_PRICE_KEY`, `WS_REALIZED_PNL_KEY`, `WS_MARK_PRICE_KEY`, `WS_MARKED_AT_KEY`, `WS_MARK_SOURCE_KEY`, `WS_OPEN_KEY`, `WS_HIGH_KEY`, `WS_LOW_KEY`, `WS_CLOSE_KEY`, `WS_VOLUME_KEY`, `WS_CLOSED_KEY`, `WS_RUN_ID_KEY`, `WS_PROVISIONAL_KEY`, `WS_TIME_KEY`, `WS_DROPPED_KEY`, `WS_QUEUED_TICKS_KEY`, `WS_QUEUED_CONTROL_KEY`, `WS_DEPTH_LEVELS_KEY`, `WS_MAX_STREAM_INSTRUMENTS_KEY`, `WS_FEED_STATE_KEY`, `WS_FEED_READY_KEY`, `WS_INSTRUMENTS_KEY`, `WS_DEPTH_KEY`, `WS_MESSAGE_KEY`, `WS_TOTAL_BARS_KEY`, `WS_SIMULATED_KEY`, `WS_WALL_SECONDS_PER_BAR_KEY`, `WS_SPEED_KEY`, `WS_BAR_INDEX_KEY`, `WS_INDEX_KEY`, `WS_TARGET_INDEX_KEY`, `WS_START_TIME_KEY`, `WS_SEED_KEY`, `WS_TICKS_PER_BAR_KEY`, `WS_MINUTES_KEY`, `WS_METHOD_KEY`, `WS_PAUSED_KEY`, `WS_SNAPSHOT_KEY`, `WS_BARS_KEY`, `WS_INDICATORS_KEY`, `WS_IDS_KEY`, `WS_MODE_KEY`, `WS_LAST_CLOSED_KEY`, `WS_CURRENT_BUCKET_KEY`, `WORKSPACE_DATA_KEY`, `WORKSPACE_REVISION_KEY`, `WORKSPACE_LAYOUT_ID_KEY`, `WORKSPACE_UPDATED_AT_KEY`, `AUTH_AUTHENTICATED_KEY`, `AUTH_SESSION_AUTH_AVAILABLE_KEY`, `AUTH_SUBJECT_KEY`, `AUTH_CSRF_TOKEN_KEY`, `LOGGED_OUT_KEY`, `PASSWORD_KEY`, `ORDER_ID_KEY`, `MODIFIED_KEY`, `CANCELLED_KEY`, `UNKNOWN_STRATEGY_KEY`, `UNKNOWN_PARAMS_KEY`, `STRATEGY_UNAVAILABLE_KEY`, `UNSUPPORTED_INTERVAL_KEY`, `SYMBOL_REQUIRED_KEY`, `NO_DATALAKE_DATA_KEY`, `UNKNOWN_SCANNER_KEY`, `WINDOW_DAYS_INTEGER_KEY`, `WINDOW_DAYS_RANGE_KEY`, `BODY_ID_REQUIRED_KEY`, `PARAMS_OBJECT_KEY`, `TRANSFORM_BARS_REQUIRED_KEY`, `PROFILE_BARS_REQUIRED_KEY`, `BODY_OBJECT_KEY`, `FROM_UTC_SECONDS_KEY`, `TO_UTC_SECONDS_KEY`, `ORDERBOOK_UNAVAILABLE_KEY`, `POSITIONS_UNAVAILABLE_KEY`, `MALFORMED_ORDER_ROW_KEY`, `ACCOUNT_UNAVAILABLE_KEY`, `NO_WORKSPACE_KEY`, `STALE_REVISION_KEY`, `LAYOUT_ID_LENGTH_KEY`, `INSTRUMENTS_NON_EMPTY_KEY`, `INVALID_JSON_KEY`, `MESSAGE_OBJECT_KEY`, `UNKNOWN_MESSAGE_TYPE_KEY`, `SUBSCRIBE_BARS_FORMAT_KEY`, `BAD_BAR_SUBSCRIPTION_KEY`, `INDICATOR_SUB_FORMAT_KEY`, `INDICATOR_UNSUB_FORMAT_KEY`, `REPLAY_START_INSTRUMENT_KEY`, `REPLAY_INSTRUMENT_FORMAT_KEY`, `UNSUPPORTED_REPLAY_INTERVAL_KEY`, `REPLAY_METHOD_KEY`, `REPLAY_DATASET_EMPTY_KEY`, `REPLAY_QUOTE_TIMESTAMP_KEY`, `REPLAY_SEEK_ACTIVE_KEY`, `REPLAY_SEEK_RESTART_FAILED_KEY`, `ORDER_STREAM_UNAVAILABLE_KEY`, `CONTROL_QUEUE_OVERFLOW_CLOSE_REASON_KEY`, `INVALID_API_KEY_CLOSE_REASON_KEY`, `AUTHENTICATION_REQUIRED_CLOSE_REASON_KEY`, `SESSION_AUTH_NOT_AVAILABLE_KEY`, `NO_LOGIN_CREDENTIALS_KEY`, `INVALID_CREDENTIALS_KEY`, `INVALID_JSON_BODY_KEY`, `RATE_LIMIT_EXCEEDED_KEY`, `CSRF_ORIGIN_MISMATCH_KEY`, `CSRF_REFERER_MISMATCH_KEY`, `CSRF_TOKEN_INVALID_KEY`, `INVALID_API_KEY_KEY`, `ERROR_CODES_KEY`, `ERROR_ENVELOPE_KEY`, `ERROR_DETAIL_KEYS_KEY`, `SERVICE_NAME_KEY`, `CLI_PROG_NAME_KEY`, `CLI_DESCRIPTION_KEY`, `FASTAPI_TITLE_KEY`, `FASTAPI_VERSION_KEY`, `SYNC_DHAN_ONLY_KEY`, `SYNC_OUTPUT_LABELS_KEY`, `CONNECTED_MESSAGES_KEY`, `ENV_FILE_LABEL_KEY`, `HEALTH_OUTPUT_LABELS_KEY`, `NO_RUNTIME_BOUND_KEY`, `NO_QUOTE_FOR_KEY`, `QUOTE_FAILED_KEY`, `ORDER_FAILED_KEY`, `WATCH_OUTPUT_LABELS_KEY`, `NO_POSITIONS_KEY`, `NO_ORDERS_KEY`, `ACCOUNT_OUTPUT_LABELS_KEY`, `POSITIONS_HEADER_KEY`, `ORDERS_HEADER_KEY`, `N_A_PLACEHOLDER_KEY`, `SERVE_FAILED_KEY`, `WORKERS_MIN_KEY`

- [ ] **Step 1: Write the failing test**

```python
# tests/domain/test_constants.py
from tradex_domain.constants import (
    SESSION_OPEN, SESSION_CLOSE, IST_TIMEZONE, DEFAULT_INITIAL_CAPITAL,
    DEFAULT_BPS_DIVISOR, DEFAULT_FILL_DEDUP_LRU, DEFAULT_TICK_SIZE,
    DEFAULT_STOP_GAP_BPS, VALUE_AREA_PERCENT, CCI_CONSTANT,
    TRADING_DAYS_PER_YEAR, EULER_MASCHERONI, BB_EXPANSION_LENGTH,
    ALMA_OFFSET, ALMA_SIGMA, T3_FACTOR, DECISION_POINT, BARS_PER_DAY,
    MAX_LOG_SIZE, STOP_TIMEOUT, SCHEDULER_INTERVAL, RECONCILIATION_INTERVAL,
    CADENCE_WINDOW, ORDER_FEED_STALE_AFTER, LEDGER_SIZE,
    FORMING_FRAME_MIN_INTERVAL, LATE_TICK_REVISION_SECONDS, DEDUP_WINDOW,
    MAX_TAPE_BYTES, MASTER_TTL_HOURS, MASTER_CLEANUP_DAYS,
    REFRESH_BUFFER_SECONDS, DHAN_REFRESH_BUFFER_MINUTES, FETCH_TIMEOUT,
    IMPERSONATE, REDIRECT_URI, PAPER_CASH, PAPER_SPREAD_BPS,
    PAPER_LEVEL_DEPTH, PAPER_LEVELS, PAPER_TICK_SIZE,
    BACKTEST_SLIPPAGE_BPS, DEFAULT_LOOKBACK, CACHE_TTL_SECONDS,
    TAIL_LIMIT, TICK_THROTTLE_SECONDS, POINTS_PER_PUSH,
    TAIL_CACHE_MAX_ENTRIES, TAIL_CACHE_MAX_AGE_SECONDS, TAIL_LOAD_JOIN_SECONDS,
    RATE_LIMIT_MAX, RATE_LIMIT_WINDOW, SESSION_ABSOLUTE_TTL, SESSION_IDLE_TTL,
    SESSION_ID_BYTES, CSRF_PURPOSE, OUTBOUND_QUEUE_MAX, CONTROL_QUEUE_MAX,
    WS_CLOSE_CONTROL_OVERFLOW, WS_CLOSE_POLICY_VIOLATION, WS_CLOSE_INTERNAL_ERROR,
    FEED_STATUS_POLL_SECONDS, HISTORY_CACHE_MAXSIZE, LRU_CACHE_MAXSIZE,
    TAIL_LOOKBACK_DAYS, REPLAY_LOOKBACK_DAYS, MIN_SPEED, MAX_SPEED,
    MIN_TICKS_PER_BAR, MAX_TICKS_PER_BAR, MIN_MINUTES, MAX_MINUTES,
    MAX_START_TIME, SLEEP_INTERVAL, DEFAULT_LIMIT, MAX_LIMIT,
    PAPER_SNAPSHOT_SPREAD, PAPER_SNAPSHOT_LOOKBACK, PAPER_LTP_CHECK,
    DEFAULT_INTERVAL_STREAM, DEFAULT_INTERVAL_CHART, DEFAULT_UNIVERSE,
    MAX_RESULTS_SMALL, MAX_RESULTS_LARGE, MAX_LIMIT_LARGE,
    BACKTEST_LOOKBACK_DAYS, DEFAULT_HOST, DEFAULT_PORT, DEFAULT_WORKERS,
    DEFAULT_BATCH_SIZE, DEFAULT_MIN_GAP_STAMPS, DEFAULT_BACKOFF_BASE,
    DEFAULT_BACKOFF_MAX, DEFAULT_SYNC_START_DAYS, BAR_FREQ_MAP,
    VERSION_DEFAULT, DEFAULT_WINDOW_DAYS, MIN_WINDOW_DAYS, MAX_WINDOW_DAYS,
    ADOPTION_TOLERANCE, FUNDS_DRIFT_THRESHOLD, RECONCILIATION_TOLERANCE,
    NO_SESSION_BOUND, FEED_NOT_READY, TAPE_RECORD_ENV_VAR,
    DHAN_COOLDOWN_SECONDS, UPSTOX_COOLDOWN_SECONDS,
    DHAN_INTRADAY_MAX_DAYS, UPSTOX_MINUTE_MAX_DAYS, MIN_MASTER_ROWS,
    DHAN_EQUITY_UNIVERSE, UPSTOX_EQUITY_UNIVERSE,
    SEGMENT_CANONICAL_DHAN, SEGMENT_CANONICAL_UPSTOX,
    _SAFE_METHODS, _SUCCESS_STATUS, PAPER_DEFAULT, MODES, READY_STATE,
    DEPTH_MODES, RECOVERY_TRIGGERS, BOOK_MATCHING, SLIPPAGE_MODEL,
    FILL_PATHS, AVG_PRICE_FALLBACK_PREFIX, BOOKED_FILL_REASON,
    WINDOW_EXPIRED_REASON, STALE_REASON, LARGE_JUMP_TRIGGER, MISSING_BAR_TRIGGER,
    ORDERS_STREAM, SYSTEM_STREAM, UNKNOWN_STATUS, HIGH_SEVERITY,
    CRITICAL_SEVERITY, LOW_SEVERITY, DRIFT_KINDS, QUANTITY_MISMATCH_REASON,
    STATUS_REASON, MISSING_REASON, BALANCE_ATTRIBUTE, EQUITY_ATTRIBUTE,
    PASSED_STATUS, NOT_PROMOTED_STATUS, NOT_EVALUATED_STATUS,
    PROMOTABLE_STATUS, NOT_PROMOTABLE_STATUS, UNREADABLE_POSITION_REASON,
    UNREADABLE_QUANTITY_REASON, UNREADABLE_INSTRUMENT_REASON,
    UNSETTLED_FILL_REASON, VENUE_OMITTED_REASON,
    UNSETTLED_GUARD_UNAVAILABLE_REASON, UNSETTLED_ORDER_STATUSES,
    VENUE_READ_FAILED_REASON, VENUE_READ_PARTIAL_REASON,
    VENUE_READ_STALE_REASON, VENUE_GET_POSITIONS_FAILED_REASON,
    UNREADABLE_INSTRUMENT_PLACEHOLDER, IDEMPOTENCY_REPLAY,
    IDEMPOTENCY_IN_FLIGHT, IDEMPOTENCY_KEY_HEADER, PRODUCT_BY_WIRE,
    PRODUCT_TYPES, ORDER_TYPES, ORDER_STATUSES, CHART_ORDER_STATUSES,
    CHART_ORDER_TYPES, STRATEGY_NAMES, STRATEGY_PARAM_NAMES,
    STRATEGY_PARAM_TYPES, STRATEGY_DEFAULTS, STRATEGY_FACTORIES,
    PROFILE_IDS, SEASONALITY_PARAMS, WORKSPACE_FIELDS, AUTH_SESSION_FIELDS,
    LOGOUT_FIELDS, LOGIN_FIELDS, ORDER_RESPONSE_FIELDS, MODIFIED_STATUS,
    CANCELLED_STATUS, SUBMITTED_STATUS, IDEMPOTENCY_REPLAY_MESSAGE,
    IDEMPOTENCY_KEY_REQUIRED_MESSAGE, IDEMPOTENCY_KEY_TOO_LONG_MESSAGE,
    ORDERS_DISABLED_DURING_REPLAY_MESSAGE, BROKER_UNAVAILABLE_MESSAGE,
    ORDER_NOT_FOUND_MESSAGE, UNKNOWN_SYMBOL_MESSAGE,
    MISSING_REQUIRED_FIELD_MESSAGE, UNKNOWN_PRODUCT_MESSAGE,
    INSTRUMENT_ID_FORMAT_MESSAGE, INSTRUMENT_ID_OR_EXCHANGE_SYMBOL_MESSAGE,
    INVALID_PRICE_MESSAGE, UNKNOWN_STRATEGY_MESSAGE, UNKNOWN_PARAMS_MESSAGE,
    STRATEGY_UNAVAILABLE_MESSAGE, UNSUPPORTED_INTERVAL_MESSAGE,
    SYMBOL_REQUIRED_MESSAGE, NO_DATALAKE_DATA_MESSAGE, UNKNOWN_SCANNER_MESSAGE,
    WINDOW_DAYS_INTEGER_MESSAGE, WINDOW_DAYS_RANGE_MESSAGE,
    BODY_ID_REQUIRED_MESSAGE, PARAMS_OBJECT_MESSAGE,
    TRANSFORM_BARS_REQUIRED_MESSAGE, PROFILE_BARS_REQUIRED_MESSAGE,
    BODY_OBJECT_MESSAGE, FROM_UTC_SECONDS_MESSAGE, TO_UTC_SECONDS_MESSAGE,
    ORDERBOOK_UNAVAILABLE_MESSAGE, POSITIONS_UNAVAILABLE_MESSAGE,
    MALFORMED_ORDER_ROW_MESSAGE, ACCOUNT_UNAVAILABLE_MESSAGE,
    NO_WORKSPACE_MESSAGE, STALE_REVISION_MESSAGE, LAYOUT_ID_LENGTH_MESSAGE,
    INSTRUMENTS_NON_EMPTY_MESSAGE, INVALID_JSON_MESSAGE, MESSAGE_OBJECT_MESSAGE,
    UNKNOWN_MESSAGE_TYPE_MESSAGE, SUBSCRIBE_BARS_FORMAT_MESSAGE,
    BAD_BAR_SUBSCRIPTION_MESSAGE, INDICATOR_SUB_FORMAT_MESSAGE,
    INDICATOR_UNSUB_FORMAT_MESSAGE, REPLAY_START_INSTRUMENT_MESSAGE,
    REPLAY_INSTRUMENT_FORMAT_MESSAGE, UNSUPPORTED_REPLAY_INTERVAL_MESSAGE,
    REPLAY_METHOD_MESSAGE, REPLAY_DATASET_EMPTY_MESSAGE,
    REPLAY_QUOTE_TIMESTAMP_MESSAGE, REPLAY_SEEK_ACTIVE_MESSAGE,
    REPLAY_SEEK_RESTART_FAILED_MESSAGE, ORDER_STREAM_UNAVAILABLE_MESSAGE,
    CONTROL_QUEUE_OVERFLOW_CLOSE_REASON, INVALID_API_KEY_CLOSE_REASON,
    AUTHENTICATION_REQUIRED_CLOSE_REASON, SESSION_AUTH_NOT_AVAILABLE_MESSAGE,
    NO_LOGIN_CREDENTIALS_MESSAGE, INVALID_CREDENTIALS_MESSAGE,
    INVALID_JSON_BODY_MESSAGE, RATE_LIMIT_EXCEEDED_MESSAGE,
    CSRF_ORIGIN_MISMATCH_MESSAGE, CSRF_REFERER_MISMATCH_MESSAGE,
    CSRF_TOKEN_INVALID_MESSAGE, INVALID_API_KEY_MESSAGE, ERROR_CODES,
    ERROR_ENVELOPE_KEY, ERROR_DETAIL_KEYS, SERVICE_NAME, CLI_PROG_NAME,
    CLI_DESCRIPTION, FASTAPI_TITLE, FASTAPI_VERSION, SYNC_DHAN_ONLY_MESSAGE,
    SYNC_OUTPUT_LABELS, CONNECTED_MESSAGES, ENV_FILE_LABEL,
    HEALTH_OUTPUT_LABELS, NO_RUNTIME_BOUND_MESSAGE, NO_QUOTE_FOR_MESSAGE,
    QUOTE_FAILED_MESSAGE, ORDER_FAILED_MESSAGE, WATCH_OUTPUT_LABELS,
    NO_POSITIONS_MESSAGE, NO_ORDERS_MESSAGE, ACCOUNT_OUTPUT_LABELS,
    POSITIONS_HEADER, ORDERS_HEADER, N_A_PLACEHOLDER, SERVE_FAILED_MESSAGE,
    WORKERS_MIN_MESSAGE, TRADE_FIELDS, METRIC_KEYS, EQUITY_CURVE_KEY,
    TRADES_KEY, METRICS_KEY, MESSAGE_KEY, TABLE_KEY, META_KEY, ID_KEY,
    POINTS_KEY, BARS_KEY, SOURCE_KEY, TIMEFRAME_RESAMPLED_KEY,
    LAST_CLOSED_TIME_KEY, SYMBOL_KEY, EXCHANGE_KEY, INTERVAL_KEY, CACHED_KEY,
    INDICATORS_KEY, CAPABILITIES_KEY, CAPABILITY_NAMES, ACCOUNT_FIELDS,
    HOLDINGS_FIELDS, POSITION_FIELDS, PORTFOLIO_KEYS, BOOK_KEYS, ORDER_FIELDS,
    NET_QTY_KEY, AVG_PRICE_KEY, ACCOUNT_ID_KEY, BALANCE_KEY, MARGIN_KEY,
    UNREALIZED_PNL_KEY, STRATEGIES_KEY, SCANNERS_KEY, BACKTESTABLE_KEY,
    STRATEGY_FIELDS, PARAM_FIELDS, SCANNER_FIELDS, SAFETY_KEYS,
    UNKNOWN_ORDER_FIELDS, RECONCILIATION_FIELDS, STRATEGY_SAFETY_FIELDS,
    FEED_STATUS_FIELDS, LIVE_ORDERS_ENABLED_KEY, WS_MESSAGE_TYPE_KEY,
    WS_INSTRUMENT_KEY, WS_LTP_KEY, WS_BID_KEY, WS_ASK_KEY, WS_BIDS_KEY,
    WS_ASKS_KEY, WS_PRICE_KEY, WS_QUANTITY_KEY, WS_ORDER_ID_KEY,
    WS_STATUS_KEY, WS_SIDE_KEY, WS_AVG_PRICE_KEY, WS_REALIZED_PNL_KEY,
    WS_MARK_PRICE_KEY, WS_MARKED_AT_KEY, WS_MARK_SOURCE_KEY, WS_OPEN_KEY,
    WS_HIGH_KEY, WS_LOW_KEY, WS_CLOSE_KEY, WS_VOLUME_KEY, WS_CLOSED_KEY,
    WS_RUN_ID_KEY, WS_PROVISIONAL_KEY, WS_TIME_KEY, WS_DROPPED_KEY,
    WS_QUEUED_TICKS_KEY, WS_QUEUED_CONTROL_KEY, WS_DEPTH_LEVELS_KEY,
    WS_MAX_STREAM_INSTRUMENTS_KEY, WS_FEED_STATE_KEY, WS_FEED_READY_KEY,
    WS_INSTRUMENTS_KEY, WS_DEPTH_KEY, WS_MESSAGE_KEY, WS_TOTAL_BARS_KEY,
    WS_SIMULATED_KEY, WS_WALL_SECONDS_PER_BAR_KEY, WS_SPEED_KEY,
    WS_BAR_INDEX_KEY, WS_INDEX_KEY, WS_TARGET_INDEX_KEY, WS_START_TIME_KEY,
    WS_SEED_KEY, WS_TICKS_PER_BAR_KEY, WS_MINUTES_KEY, WS_METHOD_KEY,
    WS_PAUSED_KEY, WS_SNAPSHOT_KEY, WS_BARS_KEY, WS_INDICATORS_KEY,
    WS_IDS_KEY, WS_MODE_KEY, WS_LAST_CLOSED_KEY, WS_CURRENT_BUCKET_KEY,
    WORKSPACE_DATA_KEY, WORKSPACE_REVISION_KEY, WORKSPACE_LAYOUT_ID_KEY,
    WORKSPACE_UPDATED_AT_KEY, AUTH_AUTHENTICATED_KEY,
    AUTH_SESSION_AUTH_AVAILABLE_KEY, AUTH_SUBJECT_KEY, AUTH_CSRF_TOKEN_KEY,
    LOGGED_OUT_KEY, PASSWORD_KEY, ORDER_ID_KEY, MODIFIED_KEY, CANCELLED_KEY,
)

def test_constants_importable():
    assert SESSION_OPEN == "09:15:00"
    assert SESSION_CLOSE == "15:29:00"
    assert DEFAULT_INITIAL_CAPITAL == 100000
    assert DEFAULT_BPS_DIVISOR == 10000
    assert DEFAULT_FILL_DEDUP_LRU == 50_000
    assert DEFAULT_TICK_SIZE == Decimal("0.05")
    assert DEFAULT_STOP_GAP_BPS == 25
    assert VALUE_AREA_PERCENT == 0.7
    assert CCI_CONSTANT == 0.015
    assert TRADING_DAYS_PER_YEAR == 252
    assert EULER_MASCHERONI == 0.5772156649015329
    assert BB_EXPANSION_LENGTH == 125
    assert ALMA_OFFSET == 0.85
    assert ALMA_SIGMA == 6.0
    assert T3_FACTOR == 0.7
    assert DECISION_POINT == "09:45"
    assert BARS_PER_DAY == 25
    assert MAX_LOG_SIZE == 10_000
    assert STOP_TIMEOUT == 5.0
    assert SCHEDULER_INTERVAL == 86_400.0
    assert RECONCILIATION_INTERVAL == 30.0
    assert CADENCE_WINDOW == 180.0
    assert ORDER_FEED_STALE_AFTER == 60.0
    assert LEDGER_SIZE == 8192
    assert FORMING_FRAME_MIN_INTERVAL == 1.0
    assert LATE_TICK_REVISION_SECONDS == 60
    assert DEDUP_WINDOW == 10_000
    assert MAX_TAPE_BYTES == 64 * 1024 * 1024
    assert MASTER_TTL_HOURS == 6.0
    assert MASTER_CLEANUP_DAYS == 7
    assert REFRESH_BUFFER_SECONDS == 1800.0
    assert DHAN_REFRESH_BUFFER_MINUTES == 10
    assert FETCH_TIMEOUT == 30.0
    assert IMPERSONATE == "chrome131"
    assert REDIRECT_URI == "http://127.0.0.1:18080/callback"
    assert PAPER_CASH == 1000000000
    assert PAPER_SPREAD_BPS == 5
    assert PAPER_LEVEL_DEPTH == 100000
    assert PAPER_LEVELS == 5
    assert PAPER_TICK_SIZE == 0.05
    assert BACKTEST_SLIPPAGE_BPS == 5
    assert DEFAULT_LOOKBACK == 10
    assert CACHE_TTL_SECONDS == 30.0
    assert TAIL_LIMIT == 400
    assert TICK_THROTTLE_SECONDS == 1.0
    assert POINTS_PER_PUSH == 2
    assert TAIL_CACHE_MAX_ENTRIES == 32
    assert TAIL_CACHE_MAX_AGE_SECONDS == 900.0
    assert TAIL_LOAD_JOIN_SECONDS == 5.0
    assert RATE_LIMIT_MAX == 20
    assert RATE_LIMIT_WINDOW == 60
    assert SESSION_ABSOLUTE_TTL == 12
    assert SESSION_IDLE_TTL == 2
    assert SESSION_ID_BYTES == 32
    assert CSRF_PURPOSE == b"csrf-v1"
    assert OUTBOUND_QUEUE_MAX == 1024
    assert CONTROL_QUEUE_MAX == 4096
    assert WS_CLOSE_CONTROL_OVERFLOW == 1013
    assert WS_CLOSE_POLICY_VIOLATION == 1008
    assert WS_CLOSE_INTERNAL_ERROR == 1011
    assert FEED_STATUS_POLL_SECONDS == 2.0
    assert HISTORY_CACHE_MAXSIZE == 256
    assert LRU_CACHE_MAXSIZE == 4
    assert TAIL_LOOKBACK_DAYS == 30
    assert REPLAY_LOOKBACK_DAYS == 3
    assert MIN_SPEED == 0.01
    assert MAX_SPEED == 1000.0
    assert MIN_TICKS_PER_BAR == 2
    assert MAX_TICKS_PER_BAR == 1000
    assert MIN_MINUTES == 1
    assert MAX_MINUTES == 10080
    assert MAX_START_TIME == 10**12
    assert SLEEP_INTERVAL == 0.01
    assert DEFAULT_LIMIT == 50
    assert MAX_LIMIT == 20000
    assert PAPER_SNAPSHOT_SPREAD == 0.05
    assert PAPER_SNAPSHOT_LOOKBACK == 2
    assert PAPER_LTP_CHECK == 100.0
    assert DEFAULT_INTERVAL_STREAM == "1m"
    assert DEFAULT_INTERVAL_CHART == "5m"
    assert DEFAULT_UNIVERSE == "nifty50"
    assert MAX_RESULTS_SMALL == 200
    assert MAX_RESULTS_LARGE == 500
    assert MAX_LIMIT_LARGE == 20000
    assert BACKTEST_LOOKBACK_DAYS == 1
    assert DEFAULT_HOST == "127.0.0.1"
    assert DEFAULT_PORT == 8080
    assert DEFAULT_WORKERS == 4
    assert DEFAULT_BATCH_SIZE == 20
    assert DEFAULT_MIN_GAP_STAMPS == 15
    assert DEFAULT_BACKOFF_BASE == 60.0
    assert DEFAULT_BACKOFF_MAX == 600.0
    assert DEFAULT_SYNC_START_DAYS == 30
    assert VERSION_DEFAULT == "1.0.0"
    assert DEFAULT_WINDOW_DAYS == 30
    assert MIN_WINDOW_DAYS == 5
    assert MAX_WINDOW_DAYS == 120
    assert ADOPTION_TOLERANCE == Decimal("0.01")
    assert FUNDS_DRIFT_THRESHOLD == Decimal("1")
    assert RECONCILIATION_TOLERANCE == Decimal("0.01")
    assert NO_SESSION_BOUND == "no session bound"
    assert FEED_NOT_READY == "feed_not_ready"
    assert TAPE_RECORD_ENV_VAR == "TRADEX_RECORD_TAPE"
    assert DHAN_COOLDOWN_SECONDS == 120.0
    assert UPSTOX_COOLDOWN_SECONDS == 600.0
    assert DHAN_INTRADAY_MAX_DAYS == 90
    assert UPSTOX_MINUTE_MAX_DAYS == 30
    assert MIN_MASTER_ROWS == 10_000
    assert PAPER_DEFAULT == "PAPER"
    assert MODES == {"paper", "backtest", "replay", "live"}
    assert READY_STATE == "READY"
    assert DEPTH_MODES == {"off", "20", "30"}
    assert BOOK_MATCHING == "book_matching"
    assert SLIPPAGE_MODEL == "slippage_model"
    assert AVG_PRICE_FALLBACK_PREFIX == "avg-price:"
    assert BOOKED_FILL_REASON == "booked_fill"
    assert WINDOW_EXPIRED_REASON == "window_expired"
    assert STALE_REASON == "stale"
    assert LARGE_JUMP_TRIGGER == "large_jump"
    assert MISSING_BAR_TRIGGER == "missing_bar"
    assert ORDERS_STREAM == "orders"
    assert SYSTEM_STREAM == "system"
    assert UNKNOWN_STATUS == "UNKNOWN"
    assert HIGH_SEVERITY == "HIGH"
    assert CRITICAL_SEVERITY == "CRITICAL"
    assert LOW_SEVERITY == "LOW"
    assert IDEMPOTENCY_REPLAY == "idempotency_replay"
    assert IDEMPOTENCY_IN_FLIGHT == "idempotency_in_flight"
    assert IDEMPOTENCY_KEY_HEADER == "Idempotency-Key"
    assert SUBMITTED_STATUS == "SUBMITTED"
    assert MODIFIED_STATUS == "modified"
    assert CANCELLED_STATUS == "cancelled"
    assert SERVICE_NAME == "tradex"
    assert CLI_PROG_NAME == "tradex-v4"
    assert FASTAPI_TITLE == "TradeX v4 API"
    assert FASTAPI_VERSION == "0.1.0"
    assert SYNC_DHAN_ONLY_MESSAGE == "sync is Dhan-only"
    assert NO_RUNTIME_BOUND_MESSAGE == "no runtime bound"
    assert NO_QUOTE_FOR_MESSAGE == "no quote for"
    assert QUOTE_FAILED_MESSAGE == "quote failed"
    assert ORDER_FAILED_MESSAGE == "order failed"
    assert NO_POSITIONS_MESSAGE == "No positions"
    assert NO_ORDERS_MESSAGE == "No orders"
    assert N_A_PLACEHOLDER == "N/A"
    assert SERVE_FAILED_MESSAGE == "serve failed"
    assert WORKERS_MIN_MESSAGE == "workers must be >= 1"
```

- [ ] **Step 2: Run test to verify it fails**

Run: `.venv/bin/python -m pytest tests/domain/test_constants.py -v`
Expected: FAIL with "ModuleNotFoundError: No module named 'tradex_domain.constants'"

- [ ] **Step 3: Write minimal implementation**

Create `domain/src/tradex_domain/constants.py` with all the constants listed above, organized by category (session, trading, broker, indicator, metric, error, websocket, etc.).

- [ ] **Step 4: Run test to verify it passes**

Run: `.venv/bin/python -m pytest tests/domain/test_constants.py -v`
Expected: PASS

- [ ] **Step 5: Commit**

```bash
git add domain/src/tradex_domain/constants.py tests/domain/test_constants.py
git commit -m "feat(domain): add central constants module

Single source of truth for all shared literals across the codebase.
Eliminates 300+ scattered constant definitions.

Co-Authored-By: Agent <agent@example.com>"
```

---

### Task 0.2: Create `domain/order_factory.py` — Single Order Construction Path

**Files:**
- Create: `domain/src/tradex_domain/order_factory.py`
- Modify: `execution/src/tradex_execution/engine.py`, `execution/src/tradex_execution/fill_sources.py`, `execution/src/tradex_execution/recovery.py`, `execution/src/tradex_execution/sqlite_store.py`, `strategy/src/tradex_strategy/core/engine.py`
- Test: `tests/domain/test_order_factory.py`

**Interfaces:**
- Consumes: `Order`, `OrderRequest`, `OrderType`, `OrderStatus`, `TimeInForce` from `domain/`
- Produces: `create_order(request: OrderRequest, order_id: str, ...) -> Order`

- [ ] **Step 1: Write the failing test**

```python
# tests/domain/test_order_factory.py
from tradex_domain.order_factory import create_order
from tradex_domain.market import OrderRequest, OrderType, OrderStatus, TimeInForce

def test_create_order_basic():
    req = OrderRequest(
        instrument_id="NSE:RELIANCE",
        side="BUY",
        quantity=10,
        order_type=OrderType.MARKET,
    )
    order = create_order(req, order_id="ord-1")
    assert order.order_id == "ord-1"
    assert order.instrument_id == "NSE:RELIANCE"
    assert order.side == "BUY"
    assert order.quantity == 10
    assert order.order_type == OrderType.MARKET
    assert order.status == OrderStatus.NEW
    assert order.time_in_force == TimeInForce.DAY

def test_create_order_with_protective():
    req = OrderRequest(
        instrument_id="NSE:RELIANCE",
        side="BUY",
        quantity=10,
        order_type=OrderType.MARKET,
        stop_loss_price=Decimal("95.00"),
        target_price=Decimal("105.00"),
    )
    order = create_order(req, order_id="ord-2")
    assert order.stop_loss_price == Decimal("95.00")
    assert order.target_price == Decimal("105.00")
```

- [ ] **Step 2: Run test to verify it fails**

Run: `.venv/bin/python -m pytest tests/domain/test_order_factory.py -v`
Expected: FAIL with "ModuleNotFoundError"

- [ ] **Step 3: Write minimal implementation**

```python
# domain/src/tradex_domain/order_factory.py
from dataclasses import dataclass
from decimal import Decimal
from typing import Optional
from tradex_domain.market import Order, OrderRequest, OrderType, OrderStatus, TimeInForce

def create_order(
    request: OrderRequest,
    order_id: str,
    *,
    filled: Decimal = Decimal("0"),
    avg_price: Optional[Decimal] = None,
    status: OrderStatus = OrderStatus.NEW,
) -> Order:
    return Order(
        order_id=order_id,
        instrument_id=request.instrument_id,
        side=request.side,
        quantity=request.quantity,
        filled=filled,
        avg_price=avg_price,
        order_type=request.order_type,
        limit_price=request.limit_price,
        stop_price=request.stop_price,
        stop_loss_price=request.stop_loss_price,
        target_price=request.target_price,
        product=request.product,
        time_in_force=TimeInForce.DAY,
        status=status,
    )
```

- [ ] **Step 4: Run test to verify it passes**

Run: `.venv/bin/python -m pytest tests/domain/test_order_factory.py -v`
Expected: PASS

- [ ] **Step 5: Commit**

```bash
git add domain/src/tradex_domain/order_factory.py tests/domain/test_order_factory.py
git commit -m "feat(domain): add single order construction path

Eliminates 8+ duplicate Order() construction sites.

Co-Authored-By: Agent <agent@example.com>"
```

---

### Task 0.3: Create `domain/fill_fingerprint.py` — Single Fill Fingerprinting Path

**Files:**
- Create: `domain/src/tradex_domain/fill_fingerprint.py`
- Modify: `execution/src/tradex_execution/idempotency.py`, `execution/src/tradex_execution/sqlite_event_store.py`, `execution/src/tradex_execution/recovery.py`
- Test: `tests/domain/test_fill_fingerprint.py`

**Interfaces:**
- Consumes: `Fill` from `domain/`
- Produces: `fingerprint(fill: Fill) -> str`

- [ ] **Step 1: Write the failing test**

```python
# tests/domain/test_fill_fingerprint.py
from tradex_domain.fill_fingerprint import fingerprint
from tradex_domain.market import Fill

def test_fingerprint_basic():
    fill = Fill(
        fill_id="fill-1",
        order_id="ord-1",
        instrument_id="NSE:RELIANCE",
        side="BUY",
        quantity=Decimal("10"),
        price=Decimal("100.00"),
    )
    fp = fingerprint(fill)
    assert isinstance(fp, str)
    assert len(fp) > 0

def test_fingerprint_deterministic():
    fill = Fill(
        fill_id="fill-1",
        order_id="ord-1",
        instrument_id="NSE:RELIANCE",
        side="BUY",
        quantity=Decimal("10"),
        price=Decimal("100.00"),
    )
    assert fingerprint(fill) == fingerprint(fill)

def test_fingerprint_unique():
    fill1 = Fill(fill_id="fill-1", order_id="ord-1", instrument_id="NSE:RELIANCE", side="BUY", quantity=Decimal("10"), price=Decimal("100.00"))
    fill2 = Fill(fill_id="fill-2", order_id="ord-1", instrument_id="NSE:RELIANCE", side="BUY", quantity=Decimal("10"), price=Decimal("100.00"))
    assert fingerprint(fill1) != fingerprint(fill2)
```

- [ ] **Step 2: Run test to verify it fails**

Run: `.venv/bin/python -m pytest tests/domain/test_fill_fingerprint.py -v`
Expected: FAIL with "ModuleNotFoundError"

- [ ] **Step 3: Write minimal implementation**

```python
# domain/src/tradex_domain/fill_fingerprint.py
import hashlib
from tradex_domain.market import Fill

def fingerprint(fill: Fill) -> str:
    key = f"{fill.fill_id}:{fill.order_id}:{fill.side}:{fill.quantity}:{fill.price}"
    return hashlib.sha256(key.encode()).hexdigest()[:16]
```

- [ ] **Step 4: Run test to verify it passes**

Run: `.venv/bin/python -m pytest tests/domain/test_fill_fingerprint.py -v`
Expected: PASS

- [ ] **Step 5: Commit**

```bash
git add domain/src/tradex_domain/fill_fingerprint.py tests/domain/test_fill_fingerprint.py
git commit -m "feat(domain): add single fill fingerprinting path

Eliminates 3 duplicate fingerprint implementations.

Co-Authored-By: Agent <agent@example.com>"
```

---

### Task 0.4: Create `domain/marketing.py` — Single Mark-to-Market Path

**Files:**
- Create: `domain/src/tradex_domain/marketing.py`
- Modify: `execution/src/tradex_execution/position_accountant.py`, `execution/src/tradex_execution/mark_to_market.py`, `replay/src/tradex_replay/backtest.py`
- Test: `tests/domain/test_marketing.py`

**Interfaces:**
- Consumes: `Position`, `Quote` from `domain/`
- Produces: `select_mark(position: Position, quote: Quote) -> Decimal`, `unrealized_pnl(position: Position, mark: Decimal) -> Decimal`

- [ ] **Step 1: Write the failing test**

```python
# tests/domain/test_marketing.py
from decimal import Decimal
from tradex_domain.marketing import select_mark, unrealized_pnl
from tradex_domain.market import Position, Quote

def test_select_mark_long():
    pos = Position(instrument_id="NSE:RELIANCE", quantity=Decimal("10"), avg_price=Decimal("100.00"))
    quote = Quote(instrument_id="NSE:RELIANCE", bid=Decimal("99.00"), ask=Decimal("101.00"), ltp=Decimal("100.00"))
    assert select_mark(pos, quote) == Decimal("99.00")  # bid for long

def test_select_mark_short():
    pos = Position(instrument_id="NSE:RELIANCE", quantity=Decimal("-10"), avg_price=Decimal("100.00"))
    quote = Quote(instrument_id="NSE:RELIANCE", bid=Decimal("99.00"), ask=Decimal("101.00"), ltp=Decimal("100.00"))
    assert select_mark(pos, quote) == Decimal("101.00")  # ask for short

def test_unrealized_pnl_long():
    pos = Position(instrument_id="NSE:RELIANCE", quantity=Decimal("10"), avg_price=Decimal("100.00"))
    assert unrealized_pnl(pos, Decimal("110.00")) == Decimal("100.00")

def test_unrealized_pnl_short():
    pos = Position(instrument_id="NSE:RELIANCE", quantity=Decimal("-10"), avg_price=Decimal("100.00"))
    assert unrealized_pnl(pos, Decimal("90.00")) == Decimal("100.00")
```

- [ ] **Step 2: Run test to verify it fails**

Run: `.venv/bin/python -m pytest tests/domain/test_marketing.py -v`
Expected: FAIL with "ModuleNotFoundError"

- [ ] **Step 3: Write minimal implementation**

```python
# domain/src/tradex_domain/marketing.py
from decimal import Decimal
from tradex_domain.market import Position, Quote

def select_mark(position: Position, quote: Quote) -> Decimal:
    if position.quantity > 0:
        return quote.bid
    elif position.quantity < 0:
        return quote.ask
    return quote.ltp

def unrealized_pnl(position: Position, mark: Decimal) -> Decimal:
    return (mark - position.avg_price) * position.quantity
```

- [ ] **Step 4: Run test to verify it passes**

Run: `.venv/bin/python -m pytest tests/domain/test_marketing.py -v`
Expected: PASS

- [ ] **Step 5: Commit**

```bash
git add domain/src/tradex_domain/marketing.py tests/domain/test_marketing.py
git commit -m "feat(domain): add single mark-to-market path

Eliminates 3 duplicate mark-to-market implementations.

Co-Authored-By: Agent <agent@example.com>"
```

---

### Task 0.5: Create `domain/protective.py` — Single Protective Trigger Path

**Files:**
- Create: `domain/src/tradex_domain/protective.py`
- Modify: `replay/src/tradex_replay/backtest.py`, `strategy/src/tradex_strategy/extensions/strategies/bracket_breakout.py`
- Test: `tests/domain/test_protective.py`

**Interfaces:**
- Consumes: `Candle`, `Decimal` from `domain/`
- Produces: `stop_hit(candle: Candle, stop: Decimal, side: str) -> bool`, `target_hit(candle: Candle, target: Decimal, side: str) -> bool`

- [ ] **Step 1: Write the failing test**

```python
# tests/domain/test_protective.py
from decimal import Decimal
from tradex_domain.protective import stop_hit, target_hit
from tradex_domain.market import Candle

def test_stop_hit_long():
    candle = Candle(open=Decimal("100"), high=Decimal("105"), low=Decimal("94"), close=Decimal("96"), volume=1000)
    assert stop_hit(candle, Decimal("95.00"), "BUY") is True

def test_stop_hit_short():
    candle = Candle(open=Decimal("100"), high=Decimal("106"), low=Decimal("95"), close=Decimal("104"), volume=1000)
    assert stop_hit(candle, Decimal("105.00"), "SELL") is True

def test_target_hit_long():
    candle = Candle(open=Decimal("100"), high=Decimal("106"), low=Decimal("99"), close=Decimal("105"), volume=1000)
    assert target_hit(candle, Decimal("105.00"), "BUY") is True

def test_target_hit_short():
    candle = Candle(open=Decimal("100"), high=Decimal("101"), low=Decimal("94"), close=Decimal("95"), volume=1000)
    assert target_hit(candle, Decimal("95.00"), "SELL") is True
```

- [ ] **Step 2: Run test to verify it fails**

Run: `.venv/bin/python -m pytest tests/domain/test_protective.py -v`
Expected: FAIL with "ModuleNotFoundError"

- [ ] **Step 3: Write minimal implementation**

```python
# domain/src/tradex_domain/protective.py
from decimal import Decimal
from tradex_domain.market import Candle

def stop_hit(candle: Candle, stop: Decimal, side: str) -> bool:
    if side == "BUY":
        return candle.low <= stop
    else:
        return candle.high >= stop

def target_hit(candle: Candle, target: Decimal, side: str) -> bool:
    if side == "BUY":
        return candle.high >= target
    else:
        return candle.low <= target
```

- [ ] **Step 4: Run test to verify it passes**

Run: `.venv/bin/python -m pytest tests/domain/test_protective.py -v`
Expected: PASS

- [ ] **Step 5: Commit**

```bash
git add domain/src/tradex_domain/protective.py tests/domain/test_protective.py
git commit -m "feat(domain): add single protective trigger path

Eliminates 2 duplicate protective trigger implementations.

Co-Authored-By: Agent <agent@example.com>"
```

---

### Task 0.6: Create `domain/fill_references.py` — Single Fill Reference Path

**Files:**
- Create: `domain/src/tradex_domain/fill_references.py`
- Modify: `strategy/src/tradex_strategy/core/engine.py`, `replay/src/tradex_replay/backtest.py`, `strategy/src/tradex_strategy/core/brackets.py`
- Test: `tests/domain/test_fill_references.py`

**Interfaces:**
- Consumes: Nothing
- Produces: `FillReference` enum with `NEXT_OPEN`, `SIGNAL_CLOSE`, `BOOK_MATCHING`, `SLIPPAGE_MODEL`

- [ ] **Step 1: Write the failing test**

```python
# tests/domain/test_fill_references.py
from tradex_domain.fill_references import FillReference

def test_fill_reference_values():
    assert FillReference.NEXT_OPEN == "next_open"
    assert FillReference.SIGNAL_CLOSE == "signal_close"
    assert FillReference.BOOK_MATCHING == "book_matching"
    assert FillReference.SLIPPAGE_MODEL == "slippage_model"
```

- [ ] **Step 2: Run test to verify it fails**

Run: `.venv/bin/python -m pytest tests/domain/test_fill_references.py -v`
Expected: FAIL with "ModuleNotFoundError"

- [ ] **Step 3: Write minimal implementation**

```python
# domain/src/tradex_domain/fill_references.py
from enum import Enum

class FillReference(Enum):
    NEXT_OPEN = "next_open"
    SIGNAL_CLOSE = "signal_close"
    BOOK_MATCHING = "book_matching"
    SLIPPAGE_MODEL = "slippage_model"
```

- [ ] **Step 4: Run test to verify it passes**

Run: `.venv/bin/python -m pytest tests/domain/test_fill_references.py -v`
Expected: PASS

- [ ] **Step 5: Commit**

```bash
git add domain/src/tradex_domain/fill_references.py tests/domain/test_fill_references.py
git commit -m "feat(domain): add single fill reference path

Eliminates 3 duplicate fill reference implementations.

Co-Authored-By: Agent <agent@example.com>"
```

---

### Task 0.7: Create `domain/correlation_id.py` — Single CorrelationId Minting Path

**Files:**
- Create: `domain/src/tradex_domain/correlation_id.py`
- Modify: `strategy/src/tradex_strategy/core/engine.py`, `replay/src/tradex_replay/backtest.py`
- Test: `tests/domain/test_correlation_id.py`

**Interfaces:**
- Consumes: Nothing
- Produces: `mint_correlation_id(prefix: str, n: int) -> str`

- [ ] **Step 1: Write the failing test**

```python
# tests/domain/test_correlation_id.py
from tradex_domain.correlation_id import mint_correlation_id

def test_mint_correlation_id_basic():
    cid = mint_correlation_id("backtest", 1)
    assert cid.startswith("backtest-")
    assert "1" in cid

def test_mint_correlation_id_deterministic():
    assert mint_correlation_id("backtest", 1) == mint_correlation_id("backtest", 1)

def test_mint_correlation_id_unique():
    assert mint_correlation_id("backtest", 1) != mint_correlation_id("backtest", 2)
```

- [ ] **Step 2: Run test to verify it fails**

Run: `.venv/bin/python -m pytest tests/domain/test_correlation_id.py -v`
Expected: FAIL with "ModuleNotFoundError"

- [ ] **Step 3: Write minimal implementation**

```python
# domain/src/tradex_domain/correlation_id.py
import hashlib

def mint_correlation_id(prefix: str, n: int) -> str:
    key = f"{prefix}:{n}"
    return f"{prefix}-{hashlib.sha256(key.encode()).hexdigest()[:8]}"
```

- [ ] **Step 4: Run test to verify it passes**

Run: `.venv/bin/python -m pytest tests/domain/test_correlation_id.py -v`
Expected: PASS

- [ ] **Step 5: Commit**

```bash
git add domain/src/tradex_domain/correlation_id.py tests/domain/test_correlation_id.py
git commit -m "feat(domain): add single CorrelationId minting path

Eliminates 3 duplicate CorrelationId minting implementations.

Co-Authored-By: Agent <agent@example.com>"
```

---

### Task 0.8: Create `domain/order_id.py` — Single Order ID Minting Path

**Files:**
- Create: `domain/src/tradex_domain/order_id.py`
- Modify: `execution/src/tradex_execution/engine.py`, `execution/src/tradex_execution/fill_sources.py`
- Test: `tests/domain/test_order_id.py`

**Interfaces:**
- Consumes: Nothing
- Produces: `mint_order_id() -> str`

- [ ] **Step 1: Write the failing test**

```python
# tests/domain/test_order_id.py
from tradex_domain.order_id import mint_order_id

def test_mint_order_id_basic():
    oid = mint_order_id()
    assert isinstance(oid, str)
    assert len(oid) > 0

def test_mint_order_id_unique():
    assert mint_order_id() != mint_order_id()
```

- [ ] **Step 2: Run test to verify it fails**

Run: `.venv/bin/python -m pytest tests/domain/test_order_id.py -v`
Expected: FAIL with "ModuleNotFoundError"

- [ ] **Step 3: Write minimal implementation**

```python
# domain/src/tradex_domain/order_id.py
import uuid

def mint_order_id() -> str:
    return f"ord-{uuid.uuid4().hex[:12]}"
```

- [ ] **Step 4: Run test to verify it passes**

Run: `.venv/bin/python -m pytest tests/domain/test_order_id.py -v`
Expected: PASS

- [ ] **Step 5: Commit**

```bash
git add domain/src/tradex_domain/order_id.py tests/domain/test_order_id.py
git commit -m "feat(domain): add single order ID minting path

Eliminates 2 duplicate order ID minting implementations.

Co-Authored-By: Agent <agent@example.com>"
```

---

### Task 0.9: Create `domain/ohlcv.py` — Single OHLCV Aggregation Path

**Files:**
- Create: `domain/src/tradex_domain/ohlcv.py`
- Modify: `domain/src/tradex_domain/market.py`, `market_data/src/tradex_market_data/market_provider.py`
- Test: `tests/domain/test_ohlcv.py`

**Interfaces:**
- Consumes: `Candle` from `domain/`
- Produces: `aggregate_candles(candles: list[Candle]) -> Candle`

- [ ] **Step 1: Write the failing test**

```python
# tests/domain/test_ohlcv.py
from decimal import Decimal
from tradex_domain.ohlcv import aggregate_candles
from tradex_domain.market import Candle

def test_aggregate_candles_basic():
    candles = [
        Candle(open=Decimal("100"), high=Decimal("105"), low=Decimal("99"), close=Decimal("102"), volume=100),
        Candle(open=Decimal("102"), high=Decimal("108"), low=Decimal("101"), close=Decimal("107"), volume=200),
    ]
    result = aggregate_candles(candles)
    assert result.open == Decimal("100")
    assert result.high == Decimal("108")
    assert result.low == Decimal("99")
    assert result.close == Decimal("107")
    assert result.volume == 300
```

- [ ] **Step 2: Run test to verify it fails**

Run: `.venv/bin/python -m pytest tests/domain/test_ohlcv.py -v`
Expected: FAIL with "ModuleNotFoundError"

- [ ] **Step 3: Write minimal implementation**

```python
# domain/src/tradex_domain/ohlcv.py
from decimal import Decimal
from tradex_domain.market import Candle

def aggregate_candles(candles: list[Candle]) -> Candle:
    if not candles:
        raise ValueError("Cannot aggregate empty candle list")
    return Candle(
        open=candles[0].open,
        high=max(c.high for c in candles),
        low=min(c.low for c in candles),
        close=candles[-1].close,
        volume=sum(c.volume for c in candles),
    )
```

- [ ] **Step 4: Run test to verify it passes**

Run: `.venv/bin/python -m pytest tests/domain/test_ohlcv.py -v`
Expected: PASS

- [ ] **Step 5: Commit**

```bash
git add domain/src/tradex_domain/ohlcv.py tests/domain/test_ohlcv.py
git commit -m "feat(domain): add single OHLCV aggregation path

Eliminates 2 duplicate OHLCV aggregation implementations.

Co-Authored-By: Agent <agent@example.com>"
```

---

### Task 0.10: Create `domain/order_status.py` — Single Order Status Set Definition

**Files:**
- Create: `domain/src/tradex_domain/order_status.py`
- Modify: `domain/src/tradex_domain/execution.py`
- Test: `tests/domain/test_order_status.py`

**Interfaces:**
- Consumes: `OrderStatus` from `domain/`
- Produces: `ACTIVE_STATUSES`, `PENDING_STATUSES`, `TERMINAL_STATUSES`

- [ ] **Step 1: Write the failing test**

```python
# tests/domain/test_order_status.py
from tradex_domain.order_status import ACTIVE_STATUSES, PENDING_STATUSES, TERMINAL_STATUSES
from tradex_domain.market import OrderStatus

def test_active_statuses():
    assert OrderStatus.NEW in ACTIVE_STATUSES
    assert OrderStatus.PENDING in ACTIVE_STATUSES
    assert OrderStatus.SUBMITTED in ACTIVE_STATUSES
    assert OrderStatus.ACK in ACTIVE_STATUSES
    assert OrderStatus.PARTIALLY_FILLED in ACTIVE_STATUSES

def test_pending_statuses():
    assert OrderStatus.NEW in PENDING_STATUSES
    assert OrderStatus.PENDING in PENDING_STATUSES
    assert OrderStatus.SUBMITTED in PENDING_STATUSES
    assert OrderStatus.ACK in PENDING_STATUSES

def test_terminal_statuses():
    assert OrderStatus.FILLED in TERMINAL_STATUSES
    assert OrderStatus.CANCELLED in TERMINAL_STATUSES
    assert OrderStatus.REJECTED in TERMINAL_STATUSES
```

- [ ] **Step 2: Run test to verify it fails**

Run: `.venv/bin/python -m pytest tests/domain/test_order_status.py -v`
Expected: FAIL with "ModuleNotFoundError"

- [ ] **Step 3: Write minimal implementation**

```python
# domain/src/tradex_domain/order_status.py
from tradex_domain.market import OrderStatus

ACTIVE_STATUSES = frozenset({
    OrderStatus.NEW,
    OrderStatus.PENDING,
    OrderStatus.SUBMITTED,
    OrderStatus.ACK,
    OrderStatus.PARTIALLY_FILLED,
})

PENDING_STATUSES = frozenset({
    OrderStatus.NEW,
    OrderStatus.PENDING,
    OrderStatus.SUBMITTED,
    OrderStatus.ACK,
})

TERMINAL_STATUSES = frozenset({
    OrderStatus.FILLED,
    OrderStatus.CANCELLED,
    OrderStatus.REJECTED,
})
```

- [ ] **Step 4: Run test to verify it passes**

Run: `.venv/bin/python -m pytest tests/domain/test_order_status.py -v`
Expected: PASS

- [ ] **Step 5: Commit**

```bash
git add domain/src/tradex_domain/order_status.py tests/domain/test_order_status.py
git commit -m "feat(domain): add single order status set definition

Eliminates 3 duplicate order status set definitions.

Co-Authored-By: Agent <agent@example.com>"
```

---

### Task 0.11: Create `domain/intervals.py` — Single Interval Normalization Path

**Files:**
- Create: `domain/src/tradex_domain/intervals.py`
- Modify: `interfaces/src/tradex_interfaces/routes/stream.py`, `interfaces/src/tradex_interfaces/routes/chart.py`
- Test: `tests/domain/test_intervals.py`

**Interfaces:**
- Consumes: Nothing
- Produces: `normalize_interval(raw: str) -> str`

- [ ] **Step 1: Write the failing test**

```python
# tests/domain/test_intervals.py
from tradex_domain.intervals import normalize_interval

def test_normalize_interval_daily():
    assert normalize_interval("1d") == "D"

def test_normalize_interval_passthrough():
    assert normalize_interval("1m") == "1m"
    assert normalize_interval("5m") == "5m"
    assert normalize_interval("15m") == "15m"
    assert normalize_interval("30m") == "30m"
    assert normalize_interval("1h") == "1h"
    assert normalize_interval("D") == "D"
```

- [ ] **Step 2: Run test to verify it fails**

Run: `.venv/bin/python -m pytest tests/domain/test_intervals.py -v`
Expected: FAIL with "ModuleNotFoundError"

- [ ] **Step 3: Write minimal implementation**

```python
# domain/src/tradex_domain/intervals.py

def normalize_interval(raw: str) -> str:
    if raw == "1d":
        return "D"
    return raw
```

- [ ] **Step 4: Run test to verify it passes**

Run: `.venv/bin/python -m pytest tests/domain/test_intervals.py -v`
Expected: PASS

- [ ] **Step 5: Commit**

```bash
git add domain/src/tradex_domain/intervals.py tests/domain/test_intervals.py
git commit -m "feat(domain): add single interval normalization path

Eliminates 2 duplicate interval normalization implementations.

Co-Authored-By: Agent <agent@example.com>"
```

---

## Wave 1: Domain Layer Unification (Parallel — after Wave 0)

These tasks are independent of each other and can run in parallel. Each creates a new domain module and updates all call sites.

### Task 1.1: Unify IST Timezone
**Files:** Modify `domain/src/tradex_domain/market_calendar.py`, `runtime/src/tradex_runtime/feed_integrity.py`, `runtime/src/tradex_runtime/live_fill_bridge.py`, `runtime/src/tradex_runtime/tape_recorder.py`, `runtime/src/tradex_runtime/bar_aggregator.py`
**Interfaces:** Consumes `IST_TIMEZONE` from `domain/constants.py`
- [ ] Replace all `ZoneInfo("Asia/Kolkata")` with `IST_TIMEZONE` from constants
- [ ] Run: `grep -r "ZoneInfo.*Asia/Kolkata" --include="*.py"` returns only `domain/constants.py`
- [ ] Commit

### Task 1.2: Unify Session Rule
**Files:** Modify `market_data/src/tradex_market_data/gap_detector.py`, `domain/src/tradex_domain/trading_calendar.py`
**Interfaces:** Consumes `SESSION_OPEN`, `SESSION_CLOSE` from `domain/constants.py`
- [ ] Replace inline session checks with constants
- [ ] Run: `grep -r "09:15" --include="*.py"` returns only `domain/constants.py`
- [ ] Commit

### Task 1.3: Unify Symbol Normalization
**Files:** Modify `domain/src/tradex_domain/instruments.py`, `domain/src/tradex_domain/universe.py`, `domain/src/tradex_domain/wire.py`, `domain/src/tradex_domain/enums.py`
**Interfaces:** Consumes `normalize_symbol` from `domain/value_objects.py`
- [ ] Replace all `strip().upper()` with `normalize_symbol()`
- [ ] Run: `grep -r "strip().upper()" --include="*.py" domain/` returns nothing
- [ ] Commit

### Task 1.4: Unify `_CASH_SERIES`
**Files:** Modify `market_data/src/tradex_market_data/symbol_resolve.py`, `market_data/src/tradex_market_data/backtest_loader.py`, `research/src/tradex_research/pit.py`, `brokers/src/tradex_brokers/dhan/adapter.py`, `brokers/src/tradex_brokers/upstox/adapter.py`
**Interfaces:** Consumes `CASH_SERIES` from `domain/universe.py`
- [ ] Replace all `frozenset({"EQ", "BE"})` with `CASH_SERIES`
- [ ] Run: `grep -r "frozenset.*EQ.*BE" --include="*.py"` returns only `domain/universe.py`
- [ ] Commit

### Task 1.5: Unify SQLite PRAGMA
**Files:** Create `execution/src/tradex_execution/_sqlite.py`, modify `execution/src/tradex_execution/sqlite_store.py`, `execution/src/tradex_execution/sqlite_event_store.py`, `execution/src/tradex_execution/risk_baseline_store.py`, `execution/src/tradex_execution/validation_report_store.py`
**Interfaces:** Produces `configure_sqlite(conn)`
- [ ] Create `_sqlite.py` with `configure_sqlite()`
- [ ] Replace all PRAGMA blocks with `configure_sqlite(conn)`
- [ ] Run: `grep -r "journal_mode.*WAL" --include="*.py" execution/` returns only `_sqlite.py`
- [ ] Commit

### Task 1.6: Unify Repo Root Anchoring
**Files:** Modify `market_data/src/tradex_market_data/paths.py`, `market_data/src/tradex_market_data/backtest_loader.py`, `services/duckdb-analytics/src/duck_analytics/screener.py`, `services/duckdb-analytics/src/duck_analytics/mcp/server.py`
**Interfaces:** Consumes `REPO_ROOT` from `domain/paths.py`
- [ ] Replace all `parents[N]` with `REPO_ROOT`
- [ ] Run: `grep -r "parents\[" --include="*.py"` returns only `domain/paths.py`
- [ ] Commit

### Task 1.7: Unify Seconds-per-Timeframe
**Files:** Modify `domain/src/tradex_domain/timeframe.py`
- [ ] Remove `_SECONDS_BY_TF`, use `_TIMEFRAMES[raw]["seconds"]`
- [ ] Run: `grep -r "_SECONDS_BY_TF" --include="*.py"` returns nothing
- [ ] Commit

### Task 1.8: Unify Intraday-ness
**Files:** Modify `domain/src/tradex_domain/timeframe.py`
- [ ] Remove `DHAN_INTRADAY` and `UPSTOX_MINUTE`, use `_TIMEFRAMES[raw]["intraday"]`
- [ ] Run: `grep -r "DHAN_INTRADAY\|UPSTOX_MINUTE" --include="*.py"` returns nothing
- [ ] Commit

### Task 1.9: Unify IST Conversion
**Files:** Modify `domain/src/tradex_domain/market.py`, `market_data/src/tradex_market_data/simple_sync.py`
**Interfaces:** Consumes `to_ist_naive` from `domain/market_calendar.py`
- [ ] Replace inline IST conversion with `to_ist_naive()`
- [ ] Run: `grep -r "astimezone.*IST" --include="*.py"` returns only `market_calendar.py`
- [ ] Commit

### Task 1.10: Delete Stale Docstring
**Files:** Modify `domain/src/tradex_domain/timeframe.py`
- [ ] Remove stale docstring referencing `MAX_POLL_DAYS`
- [ ] Run: `grep -r "MAX_POLL_DAYS" --include="*.py"` returns nothing
- [ ] Commit

---

## Wave 2: Analytics Unification (Parallel — after Wave 0)

### Task 2.1: Create `analytics/_math.py` — Shared Math Helpers
**Files:** Create `analytics/src/tradex_analytics/_math.py`, modify all 15+ analytics files
**Interfaces:** Produces `closes()`, `highs()`, `lows()`, `source_values()`, `rolling_highest()`, `rolling_lowest()`, `rma()`, `sma_skip_none()`, `stdev()`, `true_range()`, `smoothing_ma()`, `seeded_ema()`, `shift()`, `highest()`, `lowest()`, `rolling_sum()`, `vol()`, `extract()`, `vwma()`
- [ ] Create `_math.py` with all shared helpers
- [ ] Replace all duplicated functions with imports from `_math.py`
- [ ] Run: `.venv/bin/python -m pytest trading/tests/analytics -q`
- [ ] Commit

### Task 2.2: Unify Round-Trip Trade Derivation
**Files:** Create `analytics/src/tradex_analytics/_round_trips.py`, modify `analytics/src/tradex_analytics/trade_metrics.py`, `analytics/src/tradex_analytics/validation_report.py`
**Interfaces:** Produces `round_trip_trades(fills)`
- [ ] Create `_round_trips.py` with single implementation
- [ ] Replace both existing implementations
- [ ] Run: `.venv/bin/python -m pytest trading/tests/analytics -q`
- [ ] Commit

### Task 2.3: Unify Tearsheet Metrics
**Files:** Modify `analytics/src/tradex_analytics/engine.py`, `analytics/src/tradex_analytics/reports.py`
- [ ] Remove `tearsheet()` from `engine.py`, import from `reports.py`
- [ ] Run: `grep -r "def tearsheet" --include="*.py" analytics/` returns only `reports.py`
- [ ] Commit

### Task 2.4: Unify Engine Selectors
**Files:** Modify `analytics/src/tradex_analytics/engine.py`
- [ ] Remove `compute()` and `report()` hardcoded selectors, use registry
- [ ] Run: `grep -r "def compute\|def report" --include="*.py" analytics/engine.py` returns nothing
- [ ] Commit

### Task 2.5: Unify Annualization Basis
**Files:** Modify `analytics/src/tradex_analytics/volatility/volatility_chop.py`
**Interfaces:** Consumes `TRADING_DAYS_PER_YEAR` from `domain/constants.py`
- [ ] Replace `365` with `TRADING_DAYS_PER_YEAR`
- [ ] Run: `grep -r "365" --include="*.py" analytics/volatility/` returns nothing
- [ ] Commit

### Task 2.6: Unify Value Area Percent
**Files:** Modify `analytics/src/tradex_analytics/profiles.py`
**Interfaces:** Consumes `VALUE_AREA_PERCENT` from `domain/constants.py`
- [ ] Replace `0.7` with `VALUE_AREA_PERCENT`
- [ ] Run: `grep -r "0\.7" --include="*.py" analytics/profiles.py` returns nothing
- [ ] Commit

### Task 2.7: Unify Indicator Default Params
**Files:** Modify all 10+ indicator modules in `analytics/src/tradex_analytics/`
**Interfaces:** Consumes `IndicatorSpec.params` from `registry.py`
- [ ] Remove defaults from function signatures, use `IndicatorSpec.params`
- [ ] Run: `.venv/bin/python -m pytest trading/tests/analytics/test_golden_parity*.py -q`
- [ ] Commit

### Task 2.8: Unify LEGACY_PARAM_ALIASES
**Files:** Modify `analytics/src/tradex_analytics/indicators.py` + 11 module files
**Interfaces:** Consumes `register_indicator` from `registry.py`
- [ ] Replace import-time mutation with class method
- [ ] Run: `grep -r "LEGACY_PARAM_ALIASES" --include="*.py" analytics/` returns only the class method
- [ ] Commit

### Task 2.9: Unify REGISTRY Global Singleton
**Files:** Modify `analytics/src/tradex_analytics/registry.py`
- [ ] Replace global singleton with dependency injection
- [ ] Run: `grep -r "REGISTRY" --include="*.py" analytics/` returns only the DI version
- [ ] Commit

### Task 2.10: Unify IndicatorSpec and register_indicator
**Files:** Modify `analytics/src/tradex_analytics/indicators.py`
**Interfaces:** Consumes `IndicatorSpec`, `register_indicator` from `registry.py`
- [ ] Remove duplicate definitions from `indicators.py`
- [ ] Run: `grep -r "class IndicatorSpec\|def register_indicator" --include="*.py" analytics/` returns only `registry.py`
- [ ] Commit

---

## Wave 3: Broker Infrastructure Unification (Parallel — after Wave 0)

### Task 3.1: Unify Depth Construction
**Files:** Modify `brokers/src/tradex_brokers/dhan/ws_streams.py`, `brokers/src/tradex_brokers/upstox/ws_streams.py`, `brokers/src/tradex_brokers/dhan/client.py`, `brokers/src/tradex_brokers/upstox/client.py`, `brokers/src/tradex_brokers/dhan/depth_parser.py`, `brokers/src/tradex_brokers/paper/adapter.py`
**Interfaces:** Consumes `build_depth` from `common/market_builders.py`
- [ ] Replace all direct `Depth()` construction with `build_depth()`
- [ ] Run: `grep -r "Depth(" --include="*.py" brokers/` returns only `market_builders.py`
- [ ] Commit

### Task 3.2: Unify Order Status Mapping
**Files:** Create `brokers/src/tradex_brokers/common/order_status.py`, modify `brokers/src/tradex_brokers/dhan/client.py`, `brokers/src/tradex_brokers/upstox/client.py`
**Interfaces:** Produces `map_order_status(raw: str) -> OrderStatus`
- [ ] Create `order_status.py` with single mapping
- [ ] Replace both existing mappings
- [ ] Run: `.venv/bin/python -m pytest trading/tests/brokers -q`
- [ ] Commit

### Task 3.3: Unify Instrument Loading
**Files:** Create `brokers/src/tradex_brokers/common/instrument_loader.py`, modify `brokers/src/tradex_brokers/dhan/master.py`, `brokers/src/tradex_brokers/dhan/instruments.py`, `brokers/src/tradex_brokers/upstox/master.py`, `brokers/src/tradex_brokers/upstox/instruments.py`
**Interfaces:** Produces `load_instruments() -> list[Instrument]`
- [ ] Create `instrument_loader.py` with single pipeline
- [ ] Replace both existing pipelines
- [ ] Run: `.venv/bin/python -m pytest trading/tests/brokers -q`
- [ ] Commit

### Task 3.4: Unify Auth Flow
**Files:** Create `brokers/src/tradex_brokers/common/auth_flow.py`, modify `brokers/src/tradex_brokers/dhan/auth_flow.py`, `brokers/src/tradex_brokers/upstox/auth_flow.py`
**Interfaces:** Produces `authenticate() -> AuthToken`
- [ ] Create `auth_flow.py` with single flow
- [ ] Replace both existing flows
- [ ] Run: `.venv/bin/python -m pytest trading/tests/brokers -q`
- [ ] Commit

### Task 3.5: Unify Rate Tables
**Files:** Create `brokers/src/tradex_brokers/common/rate_table.py`, modify `brokers/src/tradex_brokers/dhan/rate_table.py`, `brokers/src/tradex_brokers/upstox/rate_table.py`, `brokers/src/tradex_brokers/paper/rate_table.py`
**Interfaces:** Produces `get_rate_limit_config() -> RateLimitConfig`
- [ ] Create `rate_table.py` with single config
- [ ] Replace all three existing configs
- [ ] Run: `.venv/bin/python -m pytest trading/tests/brokers -q`
- [ ] Commit

### Task 3.6: Unify Capabilities
**Files:** Create `brokers/src/tradex_brokers/common/capabilities.py`, modify `brokers/src/tradex_brokers/dhan/capabilities.py`, `brokers/src/tradex_brokers/upstox/capabilities.py`, `brokers/src/tradex_brokers/paper/capabilities.py`
**Interfaces:** Produces `get_capabilities() -> Capabilities`
- [ ] Create `capabilities.py` with single declaration
- [ ] Replace all three existing declarations
- [ ] Run: `.venv/bin/python -m pytest trading/tests/brokers -q`
- [ ] Commit

### Task 3.7: Unify WebSocket Backends Access Client Registry
**Files:** Modify `brokers/src/tradex_brokers/dhan/ws_streams.py`, `brokers/src/tradex_brokers/upstox/ws_streams.py`
**Interfaces:** Consumes `InstrumentRegistry` via constructor injection
- [ ] Inject registry via constructor instead of accessing client state
- [ ] Run: `.venv/bin/python -m pytest trading/tests/brokers -q`
- [ ] Commit

### Task 3.8: Unify Adapter Lazy State Mutation
**Files:** Modify `brokers/src/tradex_brokers/dhan/adapter.py`, `brokers/src/tradex_brokers/upstox/adapter.py`
**Interfaces:** Consumes `DepthBackend`, `OrderBackend` via factory method
- [ ] Replace lazy mutation with factory method
- [ ] Run: `.venv/bin/python -m pytest trading/tests/brokers -q`
- [ ] Commit

---

## Wave 4: Execution, Strategy, Replay, Market Data, Interfaces, Runtime, DuckDB (Parallel — after Wave 0)

### Task 4.1: Unify Execution Internals
**Files:** Modify `execution/src/tradex_execution/engine.py`, `execution/src/tradex_execution/projection.py`, `execution/src/tradex_execution/order_manager.py`, `execution/src/tradex_execution/sqlite_store.py`, `execution/src/tradex_execution/fees.py`, `execution/src/tradex_execution/fill_model.py`, `execution/src/tradex_execution/slippage.py`, `execution/src/tradex_execution/position_manager.py`, `execution/src/tradex_execution/recovery.py`, `execution/src/tradex_execution/kill_switch.py`
**Interfaces:** Consumes `create_order` from `domain/order_factory.py`, `fingerprint` from `domain/fill_fingerprint.py`, `select_mark` from `domain/marketing.py`, `configure_sqlite` from `execution/_sqlite.py`
- [ ] Replace all duplicate constructions with unified functions
- [ ] Run: `.venv/bin/python -m pytest trading/tests/execution -q`
- [ ] Commit

### Task 4.2: Unify Strategy Patterns
**Files:** Modify `strategy/src/tradex_strategy/extensions/strategies/bracket_breakout.py`, `strategy/src/tradex_strategy/extensions/strategies/sma_cross.py`, `strategy/src/tradex_strategy/extensions/strategies/mean_reversion.py`, `strategy/src/tradex_strategy/extensions/strategies/multi_symbol_sma_cross.py`, `strategy/src/tradex_strategy/extensions/strategies/study_base.py`, `strategy/src/tradex_strategy/extensions/strategies/rsi_reversal.py`, `strategy/src/tradex_strategy/extensions/strategies/ema_ribbon_pullback.py`, `strategy/src/tradex_strategy/extensions/strategies/macd_cross.py`, `strategy/src/tradex_strategy/extensions/strategies/supertrend_flip.py`, `strategy/src/tradex_strategy/extensions/strategies/bollinger_breakout.py`, `strategy/src/tradex_strategy/extensions/scanners/nifty500_technical.py`, `strategy/src/tradex_strategy/core/scanner.py`, `strategy/src/tradex_strategy/core/engine.py`
**Interfaces:** Consumes `create_order` from `domain/order_factory.py`, `stop_hit` from `domain/protective.py`, `mint_correlation_id` from `domain/correlation_id.py`
- [ ] Replace all duplicate signal constructions with unified builder
- [ ] Replace all module-level singletons with registry pattern
- [ ] Run: `.venv/bin/python -m pytest trading/tests/strategy -q`
- [ ] Commit

### Task 4.3: Unify Replay Internals
**Files:** Modify `replay/src/tradex_replay/backtest.py`, `replay/src/tradex_replay/walk_forward.py`, `replay/src/tradex_replay/synthetic_ticks.py`
**Interfaces:** Consumes `select_mark` from `domain/marketing.py`, `stop_hit` from `domain/protective.py`, `mint_correlation_id` from `domain/correlation_id.py`, `DEFAULT_TICK_SIZE` from `domain/constants.py`
- [ ] Replace all duplicate implementations with unified functions
- [ ] Run: `.venv/bin/python -m pytest trading/tests/replay -q`
- [ ] Commit

### Task 4.4: Unify Market Data
**Files:** Modify `market_data/src/tradex_market_data/market_provider.py`, `market_data/src/tradex_market_data/simple_sync.py`, `market_data/src/tradex_market_data/universe.py`, `market_data/src/tradex_market_data/backtest_loader.py`, `market_data/src/tradex_market_data/parquet_storage.py`, `market_data/src/tradex_market_data/corporate_actions.py`, `market_data/src/tradex_market_data/catalog.py`, `market_data/src/tradex_market_data/gap_detector.py`, `market_data/src/tradex_market_data/parallel_fetcher.py`, `market_data/src/tradex_market_data/symbol_resolve.py`
**Interfaces:** Consumes `aggregate_candles` from `domain/ohlcv.py`, `CASH_SERIES` from `domain/universe.py`, `REPO_ROOT` from `domain/paths.py`, `BrokerId` from `domain/enums.py`
- [ ] Replace all duplicate implementations with unified functions
- [ ] Fix `adjust_series` broken on domain Candle
- [ ] Run: `.venv/bin/python -m pytest trading/tests/datalake -q`
- [ ] Commit

### Task 4.5: Unify Interfaces
**Files:** Modify `interfaces/src/tradex_interfaces/routes/stream.py`, `interfaces/src/tradex_interfaces/routes/chart.py`, `interfaces/src/tradex_interfaces/routes/orders.py`, `interfaces/src/tradex_interfaces/routes/account.py`, `interfaces/src/tradex_interfaces/routes/market_data.py`, `interfaces/src/tradex_interfaces/routes/extensions.py`, `interfaces/src/tradex_interfaces/routes/portfolio.py`, `interfaces/src/tradex_interfaces/fastapi_app.py`, `interfaces/src/tradex_interfaces/cli.py`, `interfaces/src/tradex_interfaces/_helpers.py`, `interfaces/src/tradex_interfaces/queueing.py`, `interfaces/src/tradex_interfaces/safety.py`, `interfaces/src/tradex_interfaces/auth/csrf.py`, `interfaces/src/tradex_interfaces/auth/session_store.py`, `interfaces/src/tradex_interfaces/auth/rate_limit.py`, `interfaces/src/tradex_interfaces/routes/stream_indicators.py`
**Interfaces:** Consumes all constants from `domain/constants.py`
- [ ] Replace all hardcoded literals with constants
- [ ] Run: `grep -r '"09:15"\|"15:30"\|"100000"\|"50_000"\|"0.05"\|"0.01"' --include="*.py" interfaces/` returns nothing
- [ ] Commit

### Task 4.6: Unify Runtime
**Files:** Modify `runtime/src/tradex_runtime/feed_integrity.py`, `runtime/src/tradex_runtime/live_fill_bridge.py`, `runtime/src/tradex_runtime/tape_recorder.py`, `runtime/src/tradex_runtime/bar_aggregator.py`, `runtime/src/tradex_runtime/feed_supervisor.py`, `runtime/src/tradex_runtime/feed_monitor.py`, `runtime/src/tradex_runtime/startup.py`, `runtime/src/tradex_runtime/live.py`, `runtime/src/tradex_runtime/paper_book_fill_source.py`, `runtime/src/tradex_runtime/reconciliation_monitor.py`, `runtime/src/tradex_runtime/master_lifecycle.py`, `runtime/src/tradex_runtime/feed_recovery.py`, `runtime/src/tradex_runtime/market_feed.py`
**Interfaces:** Consumes all constants from `domain/constants.py`
- [ ] Replace all hardcoded literals with constants
- [ ] Run: `grep -r '"09:15"\|"15:30"\|"100000"\|"50_000"\|"0.05"\|"0.01"' --include="*.py" runtime/` returns nothing
- [ ] Commit

### Task 4.7: Unify DuckDB Analytics
**Files:** Modify `services/duckdb-analytics/src/duck_analytics/mcp/server.py`, `services/duckdb-analytics/src/duck_analytics/catalog.py`, `services/duckdb-analytics/src/duck_analytics/screener.py`, `services/duckdb-analytics/src/duck_analytics/rs_score.py`, `services/duckdb-analytics/src/duck_analytics/scanners.py`, `services/duckdb-analytics/src/duck_analytics/patterns.py`
**Interfaces:** Consumes `REPO_ROOT` from `domain/paths.py`, all constants from `domain/constants.py`
- [ ] Replace all duplicate implementations with unified functions
- [ ] Run: `.venv/bin/python -m pytest trading/tests/duckdb -q`
- [ ] Commit

### Task 4.8: Unify Reactive
**Files:** Modify `reactive/src/tradex_reactive/bus.py`, `reactive/src/tradex_reactive/thread_safe_bus.py`
**Interfaces:** Consumes `MAX_LOG_SIZE` from `domain/constants.py`
- [ ] Replace `10_000` with `MAX_LOG_SIZE`
- [ ] Run: `grep -r "10_000" --include="*.py" reactive/` returns nothing
- [ ] Commit

### Task 4.9: Unify Observability
**Files:** Modify `observability/src/tradex_observability/metrics.py`
**Interfaces:** Consumes all metric name constants from `domain/constants.py`
- [ ] Replace all hardcoded metric names with constants
- [ ] Run: `grep -r '"feed_recovery_failures_total"\|"feed_resync_duration_seconds"\|"feed_gaps_total"' --include="*.py" observability/` returns nothing
- [ ] Commit

### Task 4.10: Unify Config
**Files:** Modify `config/src/tradex_config/env.py`, `config/src/tradex_config/schema.py`
**Interfaces:** Consumes all constants from `domain/constants.py`
- [ ] Replace all hardcoded literals with constants
- [ ] Run: `grep -r '"PAPER"\|"next_open"\|"signal_close"\|"book_matching"\|"slippage_model"' --include="*.py" config/` returns nothing
- [ ] Commit

---

## Wave 5: Cross-Cutting Concerns (Parallel — after Waves 1-4)

### Task 5.1: Introduce Service/Use-Case Layer
**Files:** Create `application/src/tradex_application/bracket_service.py`, `application/src/tradex_application/fill_service.py`, `application/src/tradex_application/sizing_service.py`
**Interfaces:** Consumes `create_order` from `domain/order_factory.py`, `fingerprint` from `domain/fill_fingerprint.py`, `select_mark` from `domain/marketing.py`
- [ ] Create service layer with single bracket order placement path
- [ ] Create service layer with single fill computation path
- [ ] Create service layer with single sizing path
- [ ] Run: `.venv/bin/python -m pytest trading/tests/parity -q`
- [ ] Commit

### Task 5.2: Enforce Boundary Rules
**Files:** All packages
**Interfaces:** Consumes boundary rules from this plan
- [ ] Add `import-linter` configuration to enforce boundary rules
- [ ] Add pre-commit hooks to check for hardcoded literals
- [ ] Add `__all__` to all public modules
- [ ] Add complete type annotations to all public functions
- [ ] Run: `.venv/bin/python -m pytest trading/tests/test_complexity_gate.py -q`
- [ ] Commit

### Task 5.3: Establish Coding Standards
**Files:** All packages
**Interfaces:** Consumes coding standards from this plan
- [ ] Add `mypy` configuration to enforce type annotations
- [ ] Add `ruff` configuration to enforce style
- [ ] Add coverage floors to CI
- [ ] Run: `.venv/bin/python -m pytest trading/tests/ -q`
- [ ] Commit

### Task 5.4: Add Guardrails
**Files:** `.pre-commit-config.yaml`, `pyproject.toml`, `.github/workflows/ci.yml`
**Interfaces:** Consumes guardrails from this plan
- [ ] Add import-linter to CI
- [ ] Add pre-commit hooks for hardcoded literal detection
- [ ] Add complexity gate to CI
- [ ] Add coverage floors to CI
- [ ] Run: `.venv/bin/python -m pytest trading/tests/ -q`
- [ ] Commit

---

## Execution Strategy

### Multi-Agent Parallel Execution

**Wave 0 (Sequential):** Tasks 0.1-0.11 must complete first. Each task is dispatched to a fresh implementer subagent, reviewed, then committed.

**Wave 1 (Parallel):** Tasks 1.1-1.10 are independent. Dispatch all 10 implementer subagents in parallel. Review each as they complete.

**Wave 2 (Parallel):** Tasks 2.1-2.10 are independent. Dispatch all 10 implementer subagents in parallel.

**Wave 3 (Parallel):** Tasks 3.1-3.8 are independent. Dispatch all 8 implementer subagents in parallel.

**Wave 4 (Parallel):** Tasks 4.1-4.10 are independent. Dispatch all 10 implementer subagents in parallel.

**Wave 5 (Parallel):** Tasks 5.1-5.4 are independent. Dispatch all 4 implementer subagents in parallel.

### Agent Assignment

| Agent | Wave | Tasks | Model |
|-------|------|-------|-------|
| Implementer 1 | 0 | 0.1-0.11 | Standard |
| Implementer 2-11 | 1 | 1.1-1.10 | Cheap |
| Implementer 12-21 | 2 | 2.1-2.10 | Standard |
| Implementer 22-29 | 3 | 3.1-3.8 | Standard |
| Implementer 30-39 | 4 | 4.1-4.10 | Standard |
| Implementer 40-43 | 5 | 5.1-5.4 | Standard |
| Reviewer 1-43 | All | All | Standard |
| Final Reviewer | End | All | Most capable |

### Progress Ledger

Track progress in `.superpowers/sdd/progress.md`:

```
Task 0.1: complete (commits abc1234..def5678, review clean)
Task 0.2: complete (commits ghi9012..jkl3456, review clean)
...
```

### Verification

After each wave:
1. Run full test suite: `.venv/bin/python -m pytest trading/tests/ -q`
2. Run complexity gate: `.venv/bin/python -m pytest trading/tests/test_complexity_gate.py -q`
3. Run coverage floors: `python scripts/coverage_floors.py`
4. Run import-linter: `lint-imports`
5. Run mypy: `mypy domain/ config/ market_data/ brokers/ execution/ replay/ strategy/ runtime/ interfaces/ application/ analytics/ services/ reactive/ observability/`

After all waves:
1. Run full test suite
2. Run complexity gate
3. Run coverage floors
4. Run import-linter
5. Run mypy
6. Run final code review
7. Merge to main
