from decimal import Decimal
from zoneinfo import ZoneInfo

import pytest

from tradex_domain import constants as C


class TestSessionConstants:
    def test_session_open(self):
        assert C.SESSION_OPEN == "09:15:00"

    def test_session_close(self):
        assert C.SESSION_CLOSE == "15:29:00"

    def test_ist_timezone(self):
        assert C.IST_TIMEZONE == ZoneInfo("Asia/Kolkata")


class TestTradingConstants:
    def test_default_initial_capital(self):
        assert C.DEFAULT_INITIAL_CAPITAL == Decimal("100000")

    def test_default_bps_divisor(self):
        assert C.DEFAULT_BPS_DIVISOR == Decimal("10000")

    def test_default_fill_dedup_lru(self):
        assert C.DEFAULT_FILL_DEDUP_LRU == 50_000

    def test_default_tick_size(self):
        assert C.DEFAULT_TICK_SIZE == Decimal("0.05")

    def test_default_stop_gap_bps(self):
        assert C.DEFAULT_STOP_GAP_BPS == 25

    def test_default_backtest_risk_fraction(self):
        assert C.DEFAULT_BACKTEST_RISK_FRACTION == 0.01

    def test_default_warmup_bars(self):
        assert C.DEFAULT_WARMUP_BARS == 21

    def test_embargo_fraction(self):
        assert C.EMBARGO_FRACTION == 0.01

    def test_value_area_percent(self):
        assert C.VALUE_AREA_PERCENT == 0.7

    def test_cci_constant(self):
        assert C.CCI_CONSTANT == 0.015

    def test_trading_days_per_year(self):
        assert C.TRADING_DAYS_PER_YEAR == 252

    def test_euler_mascheroni(self):
        assert C.EULER_MASCHERONI == 0.5772156649015329

    def test_bb_expansion_length(self):
        assert C.BB_EXPANSION_LENGTH == 125

    def test_alma_offset(self):
        assert C.ALMA_OFFSET == 0.85

    def test_alma_sigma(self):
        assert C.ALMA_SIGMA == 6.0

    def test_t3_factor(self):
        assert C.T3_FACTOR == 0.7

    def test_decision_point(self):
        assert C.DECISION_POINT == "09:45"

    def test_bars_per_day(self):
        assert C.BARS_PER_DAY == 25

    def test_crypto_bars_per_day(self):
        assert C.CRYPTO_BARS_PER_DAY == 96


class TestTimeoutConstants:
    def test_max_log_size(self):
        assert C.MAX_LOG_SIZE == 10_000

    def test_stop_timeout(self):
        assert C.STOP_TIMEOUT == 5.0

    def test_scheduler_interval(self):
        assert C.SCHEDULER_INTERVAL == 86_400.0

    def test_reconciliation_interval(self):
        assert C.RECONCILIATION_INTERVAL == 30.0

    def test_cadence_window(self):
        assert C.CADENCE_WINDOW == 180.0

    def test_order_feed_stale_after(self):
        assert C.ORDER_FEED_STALE_AFTER == 60.0

    def test_ledger_size(self):
        assert C.LEDGER_SIZE == 8192

    def test_forming_frame_min_interval(self):
        assert C.FORMING_FRAME_MIN_INTERVAL == 1.0

    def test_late_tick_revision_seconds(self):
        assert C.LATE_TICK_REVISION_SECONDS == 60

    def test_dedup_window(self):
        assert C.DEDUP_WINDOW == 10_000

    def test_max_tape_bytes(self):
        assert C.MAX_TAPE_BYTES == 64 * 1024 * 1024

    def test_master_ttl_hours(self):
        assert C.MASTER_TTL_HOURS == 6.0

    def test_master_cleanup_days(self):
        assert C.MASTER_CLEANUP_DAYS == 7

    def test_refresh_buffer_seconds(self):
        assert C.REFRESH_BUFFER_SECONDS == 1800.0

    def test_dhan_refresh_buffer_minutes(self):
        assert C.DHAN_REFRESH_BUFFER_MINUTES == 10

    def test_fetch_timeout(self):
        assert C.FETCH_TIMEOUT == 30.0

    def test_impersonate(self):
        assert C.IMPERSONATE == "chrome131"

    def test_redirect_uri(self):
        assert C.REDIRECT_URI == "http://127.0.0.1:18080/callback"

    def test_paper_cash(self):
        assert C.PAPER_CASH == Decimal("1000000000")

    def test_paper_spread_bps(self):
        assert C.PAPER_SPREAD_BPS == 5

    def test_paper_level_depth(self):
        assert C.PAPER_LEVEL_DEPTH == 100000

    def test_paper_levels(self):
        assert C.PAPER_LEVELS == 5

    def test_paper_tick_size(self):
        assert C.PAPER_TICK_SIZE == Decimal("0.05")

    def test_backtest_slippage_bps(self):
        assert C.BACKTEST_SLIPPAGE_BPS == 5

    def test_default_lookback(self):
        assert C.DEFAULT_LOOKBACK == 10

    def test_cache_ttl_seconds(self):
        assert C.CACHE_TTL_SECONDS == 30.0

    def test_tail_limit(self):
        assert C.TAIL_LIMIT == 400

    def test_tick_throttle_seconds(self):
        assert C.TICK_THROTTLE_SECONDS == 1.0

    def test_points_per_push(self):
        assert C.POINTS_PER_PUSH == 2

    def test_tail_cache_max_entries(self):
        assert C.TAIL_CACHE_MAX_ENTRIES == 32

    def test_tail_cache_max_age_seconds(self):
        assert C.TAIL_CACHE_MAX_AGE_SECONDS == 900.0

    def test_tail_load_join_seconds(self):
        assert C.TAIL_LOAD_JOIN_SECONDS == 5.0

    def test_rate_limit_max(self):
        assert C.RATE_LIMIT_MAX == 20

    def test_rate_limit_window(self):
        assert C.RATE_LIMIT_WINDOW == 60

    def test_session_absolute_ttl(self):
        assert C.SESSION_ABSOLUTE_TTL == 12

    def test_session_idle_ttl(self):
        assert C.SESSION_IDLE_TTL == 2

    def test_session_id_bytes(self):
        assert C.SESSION_ID_BYTES == 32

    def test_csrf_purpose(self):
        assert C.CSRF_PURPOSE == b"csrf-v1"

    def test_outbound_queue_max(self):
        assert C.OUTBOUND_QUEUE_MAX == 1024

    def test_control_queue_max(self):
        assert C.CONTROL_QUEUE_MAX == 4096

    def test_ws_close_control_overflow(self):
        assert C.WS_CLOSE_CONTROL_OVERFLOW == 1013

    def test_ws_close_policy_violation(self):
        assert C.WS_CLOSE_POLICY_VIOLATION == 1008

    def test_ws_close_internal_error(self):
        assert C.WS_CLOSE_INTERNAL_ERROR == 1011

    def test_feed_status_poll_seconds(self):
        assert C.FEED_STATUS_POLL_SECONDS == 2.0

    def test_history_cache_maxsize(self):
        assert C.HISTORY_CACHE_MAXSIZE == 256

    def test_lru_cache_maxsize(self):
        assert C.LRU_CACHE_MAXSIZE == 4

    def test_tail_lookback_days(self):
        assert C.TAIL_LOOKBACK_DAYS == 30

    def test_replay_lookback_days(self):
        assert C.REPLAY_LOOKBACK_DAYS == 3

    def test_min_speed(self):
        assert C.MIN_SPEED == 0.01

    def test_max_speed(self):
        assert C.MAX_SPEED == 1000.0

    def test_min_ticks_per_bar(self):
        assert C.MIN_TICKS_PER_BAR == 2

    def test_max_ticks_per_bar(self):
        assert C.MAX_TICKS_PER_BAR == 1000

    def test_min_minutes(self):
        assert C.MIN_MINUTES == 1

    def test_max_minutes(self):
        assert C.MAX_MINUTES == 10080

    def test_max_start_time(self):
        assert C.MAX_START_TIME == 10**12

    def test_sleep_interval(self):
        assert C.SLEEP_INTERVAL == 0.01

    def test_default_limit(self):
        assert C.DEFAULT_LIMIT == 50

    def test_max_limit(self):
        assert C.MAX_LIMIT == 20000

    def test_paper_snapshot_spread(self):
        assert C.PAPER_SNAPSHOT_SPREAD == Decimal("0.05")

    def test_paper_snapshot_lookback(self):
        assert C.PAPER_SNAPSHOT_LOOKBACK == 2

    def test_paper_ltp_check(self):
        assert C.PAPER_LTP_CHECK == 100.0

    def test_default_interval_stream(self):
        assert C.DEFAULT_INTERVAL_STREAM == "1m"

    def test_default_interval_chart(self):
        assert C.DEFAULT_INTERVAL_CHART == "5m"

    def test_default_universe(self):
        assert C.DEFAULT_UNIVERSE == "nifty50"

    def test_max_results_small(self):
        assert C.MAX_RESULTS_SMALL == 200

    def test_max_results_large(self):
        assert C.MAX_RESULTS_LARGE == 500

    def test_max_limit_large(self):
        assert C.MAX_LIMIT_LARGE == 20000

    def test_backtest_lookback_days(self):
        assert C.BACKTEST_LOOKBACK_DAYS == 1

    def test_default_host(self):
        assert C.DEFAULT_HOST == "127.0.0.1"

    def test_default_port(self):
        assert C.DEFAULT_PORT == 8080

    def test_default_workers(self):
        assert C.DEFAULT_WORKERS == 4

    def test_default_batch_size(self):
        assert C.DEFAULT_BATCH_SIZE == 20

    def test_default_min_gap_stamps(self):
        assert C.DEFAULT_MIN_GAP_STAMPS == 15

    def test_default_backoff_base(self):
        assert C.DEFAULT_BACKOFF_BASE == 60.0

    def test_default_backoff_max(self):
        assert C.DEFAULT_BACKOFF_MAX == 600.0

    def test_default_sync_start_days(self):
        assert C.DEFAULT_SYNC_START_DAYS == 30

    def test_bar_freq_map(self):
        assert C.BAR_FREQ_MAP == {"1m": "1min", "5m": "5min", "15m": "15min"}

    def test_version_default(self):
        assert C.VERSION_DEFAULT == "1.0.0"

    def test_default_window_days(self):
        assert C.DEFAULT_WINDOW_DAYS == 30

    def test_min_window_days(self):
        assert C.MIN_WINDOW_DAYS == 5

    def test_max_window_days(self):
        assert C.MAX_WINDOW_DAYS == 120

    def test_adoption_tolerance(self):
        assert C.ADOPTION_TOLERANCE == Decimal("0.01")

    def test_funds_drift_threshold(self):
        assert C.FUNDS_DRIFT_THRESHOLD == Decimal("1")

    def test_reconciliation_tolerance(self):
        assert C.RECONCILIATION_TOLERANCE == Decimal("0.01")


class TestErrorMessages:
    def test_no_session_bound(self):
        assert C.NO_SESSION_BOUND == "no session bound"

    def test_feed_not_ready(self):
        assert C.FEED_NOT_READY == "feed_not_ready"

    def test_tape_record_env_var(self):
        assert C.TAPE_RECORD_ENV_VAR == "TRADEX_RECORD_TAPE"

    def test_dhan_cooldown_seconds(self):
        assert C.DHAN_COOLDOWN_SECONDS == 120.0

    def test_upstox_cooldown_seconds(self):
        assert C.UPSTOX_COOLDOWN_SECONDS == 600.0

    def test_dhan_intraday_max_days(self):
        assert C.DHAN_INTRADAY_MAX_DAYS == 90

    def test_upstox_minute_max_days(self):
        assert C.UPSTOX_MINUTE_MAX_DAYS == 30

    def test_min_master_rows(self):
        assert C.MIN_MASTER_ROWS == 10_000

    def test_paper_default(self):
        assert C.PAPER_DEFAULT == "PAPER"

    def test_modes(self):
        assert C.MODES == {"paper", "backtest", "replay", "live"}

    def test_ready_state(self):
        assert C.READY_STATE == "READY"

    def test_depth_modes(self):
        assert C.DEPTH_MODES == {"off", "20", "30"}

    def test_recovery_triggers(self):
        assert C.RECOVERY_TRIGGERS == {"large_jump", "missing_bar"}

    def test_book_matching(self):
        assert C.BOOK_MATCHING == "book_matching"

    def test_slippage_model(self):
        assert C.SLIPPAGE_MODEL == "slippage_model"

    def test_fill_paths(self):
        assert C.FILL_PATHS == {"book_matching", "slippage_model"}

    def test_avg_price_fallback_prefix(self):
        assert C.AVG_PRICE_FALLBACK_PREFIX == "avg-price:"

    def test_booked_fill_reason(self):
        assert C.BOOKED_FILL_REASON == "booked_fill"

    def test_window_expired_reason(self):
        assert C.WINDOW_EXPIRED_REASON == "window_expired"

    def test_stale_reason(self):
        assert C.STALE_REASON == "stale"

    def test_large_jump_trigger(self):
        assert C.LARGE_JUMP_TRIGGER == "large_jump"

    def test_missing_bar_trigger(self):
        assert C.MISSING_BAR_TRIGGER == "missing_bar"

    def test_orders_stream(self):
        assert C.ORDERS_STREAM == "orders"

    def test_system_stream(self):
        assert C.SYSTEM_STREAM == "system"

    def test_unknown_status(self):
        assert C.UNKNOWN_STATUS == "UNKNOWN"

    def test_high_severity(self):
        assert C.HIGH_SEVERITY == "HIGH"

    def test_critical_severity(self):
        assert C.CRITICAL_SEVERITY == "CRITICAL"

    def test_low_severity(self):
        assert C.LOW_SEVERITY == "LOW"

    def test_drift_kinds(self):
        assert C.DRIFT_KINDS == {"position", "order", "funds"}

    def test_quantity_mismatch_reason(self):
        assert C.QUANTITY_MISMATCH_REASON == "quantity mismatch"

    def test_status_reason(self):
        assert C.STATUS_REASON == "status"

    def test_missing_reason(self):
        assert C.MISSING_REASON == "missing"

    def test_balance_attribute(self):
        assert C.BALANCE_ATTRIBUTE == "balance"

    def test_equity_attribute(self):
        assert C.EQUITY_ATTRIBUTE == "equity"

    def test_passed_status(self):
        assert C.PASSED_STATUS == "passed"

    def test_not_promoted_status(self):
        assert C.NOT_PROMOTED_STATUS == "not_promoted"

    def test_not_evaluated_status(self):
        assert C.NOT_EVALUATED_STATUS == "not_evaluated"

    def test_promotable_status(self):
        assert C.PROMOTABLE_STATUS == "promotable"

    def test_not_promotable_status(self):
        assert C.NOT_PROMOTABLE_STATUS == "not_promotable"

    def test_unreadable_position_reason(self):
        assert C.UNREADABLE_POSITION_REASON == "unreadable_position"

    def test_unreadable_quantity_reason(self):
        assert C.UNREADABLE_QUANTITY_REASON == "unreadable_quantity"

    def test_unreadable_instrument_reason(self):
        assert C.UNREADABLE_INSTRUMENT_REASON == "unreadable_instrument"

    def test_unsettled_fill_reason(self):
        assert C.UNSETTLED_FILL_REASON == "unsettled_fill"

    def test_venue_omitted_reason(self):
        assert C.VENUE_OMITTED_REASON == "venue_omitted"

    def test_unsettled_guard_unavailable_reason(self):
        assert C.UNSETTLED_GUARD_UNAVAILABLE_REASON == "unsettled_guard_unavailable"

    def test_venue_read_failed_reason(self):
        assert C.VENUE_READ_FAILED_REASON == "venue_read_failed"

    def test_venue_read_partial_reason(self):
        assert C.VENUE_READ_PARTIAL_REASON == "venue_read_partial"

    def test_venue_read_stale_reason(self):
        assert C.VENUE_READ_STALE_REASON == "venue_read_stale"

    def test_venue_get_positions_failed_reason(self):
        assert C.VENUE_GET_POSITIONS_FAILED_REASON == "venue_get_positions_failed"

    def test_unreadable_instrument_placeholder(self):
        assert C.UNREADABLE_INSTRUMENT_PLACEHOLDER == "<unreadable:instrument>"

    def test_idempotency_replay(self):
        assert C.IDEMPOTENCY_REPLAY == "idempotency_replay"

    def test_idempotency_in_flight(self):
        assert C.IDEMPOTENCY_IN_FLIGHT == "idempotency_in_flight"

    def test_idempotency_key_header(self):
        assert C.IDEMPOTENCY_KEY_HEADER == "Idempotency-Key"

    def test_submitted_status(self):
        assert C.SUBMITTED_STATUS == "SUBMITTED"

    def test_modified_status(self):
        assert C.MODIFIED_STATUS == "modified"

    def test_cancelled_status(self):
        assert C.CANCELLED_STATUS == "cancelled"

    def test_service_name(self):
        assert C.SERVICE_NAME == "tradex"

    def test_cli_prog_name(self):
        assert C.CLI_PROG_NAME == "tradex-v4"

    def test_cli_description(self):
        assert C.CLI_DESCRIPTION == "TradeX v4 Trading Platform"

    def test_fastapi_title(self):
        assert C.FASTAPI_TITLE == "TradeX v4 API"

    def test_fastapi_version(self):
        assert C.FASTAPI_VERSION == "0.1.0"

    def test_sync_dhan_only_message(self):
        assert C.SYNC_DHAN_ONLY_MESSAGE == "sync is Dhan-only"

    def test_sync_output_labels(self):
        assert C.SYNC_OUTPUT_LABELS == {
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

    def test_connected_messages(self):
        assert C.CONNECTED_MESSAGES == {
            "paper": "connected: paper (dry-run)",
            "dhan": "connected: dhan",
        }

    def test_env_file_label(self):
        assert C.ENV_FILE_LABEL == "env_file="

    def test_health_output_labels(self):
        assert C.HEALTH_OUTPUT_LABELS == {"session": "session=", "environment": "environment="}

    def test_no_runtime_bound_message(self):
        assert C.NO_RUNTIME_BOUND_MESSAGE == "no runtime bound"

    def test_no_quote_for_message(self):
        assert C.NO_QUOTE_FOR_MESSAGE == "no quote for"

    def test_quote_failed_message(self):
        assert C.QUOTE_FAILED_MESSAGE == "quote failed"

    def test_order_failed_message(self):
        assert C.ORDER_FAILED_MESSAGE == "order failed"

    def test_watch_output_labels(self):
        assert C.WATCH_OUTPUT_LABELS == {
            "watching": "Watching",
            "ltp": "LTP:",
            "no_quote": "No quote available",
            "error": "Error:",
        }

    def test_no_positions_message(self):
        assert C.NO_POSITIONS_MESSAGE == "No positions"

    def test_no_orders_message(self):
        assert C.NO_ORDERS_MESSAGE == "No orders"

    def test_account_output_labels(self):
        assert C.ACCOUNT_OUTPUT_LABELS == {"account": "Account:", "balance": "Balance:"}

    def test_positions_header(self):
        assert C.POSITIONS_HEADER == ["Symbol", "Qty", "Avg Price", "LTP", "P&L"]

    def test_orders_header(self):
        assert C.ORDERS_HEADER == ["Order ID", "Symbol", "Side", "Status", "Qty"]

    def test_n_a_placeholder(self):
        assert C.N_A_PLACEHOLDER == "N/A"

    def test_serve_failed_message(self):
        assert C.SERVE_FAILED_MESSAGE == "serve failed"

    def test_workers_min_message(self):
        assert C.WORKERS_MIN_MESSAGE == "workers must be >= 1"


class TestWebSocketConstants:
    def test_ws_message_type_key(self):
        assert C.WS_MESSAGE_TYPE_KEY == "type"

    def test_ws_instrument_key(self):
        assert C.WS_INSTRUMENT_KEY == "instrument"

    def test_ws_ltp_key(self):
        assert C.WS_LTP_KEY == "ltp"

    def test_ws_bid_key(self):
        assert C.WS_BID_KEY == "bid"

    def test_ws_ask_key(self):
        assert C.WS_ASK_KEY == "ask"

    def test_ws_bids_key(self):
        assert C.WS_BIDS_KEY == "bids"

    def test_ws_asks_key(self):
        assert C.WS_ASKS_KEY == "asks"

    def test_ws_price_key(self):
        assert C.WS_PRICE_KEY == "price"

    def test_ws_quantity_key(self):
        assert C.WS_QUANTITY_KEY == "quantity"

    def test_ws_order_id_key(self):
        assert C.WS_ORDER_ID_KEY == "order_id"

    def test_ws_status_key(self):
        assert C.WS_STATUS_KEY == "status"

    def test_ws_side_key(self):
        assert C.WS_SIDE_KEY == "side"

    def test_ws_avg_price_key(self):
        assert C.WS_AVG_PRICE_KEY == "avg_price"

    def test_ws_realized_pnl_key(self):
        assert C.WS_REALIZED_PNL_KEY == "realized_pnl"

    def test_ws_mark_price_key(self):
        assert C.WS_MARK_PRICE_KEY == "mark_price"

    def test_ws_marked_at_key(self):
        assert C.WS_MARKED_AT_KEY == "marked_at"

    def test_ws_mark_source_key(self):
        assert C.WS_MARK_SOURCE_KEY == "mark_source"

    def test_ws_open_key(self):
        assert C.WS_OPEN_KEY == "open"

    def test_ws_high_key(self):
        assert C.WS_HIGH_KEY == "high"

    def test_ws_low_key(self):
        assert C.WS_LOW_KEY == "low"

    def test_ws_close_key(self):
        assert C.WS_CLOSE_KEY == "close"

    def test_ws_volume_key(self):
        assert C.WS_VOLUME_KEY == "volume"

    def test_ws_closed_key(self):
        assert C.WS_CLOSED_KEY == "closed"

    def test_ws_run_id_key(self):
        assert C.WS_RUN_ID_KEY == "run_id"

    def test_ws_provisional_key(self):
        assert C.WS_PROVISIONAL_KEY == "provisional"

    def test_ws_time_key(self):
        assert C.WS_TIME_KEY == "time"

    def test_ws_dropped_key(self):
        assert C.WS_DROPPED_KEY == "dropped"

    def test_ws_queued_ticks_key(self):
        assert C.WS_QUEUED_TICKS_KEY == "queued_ticks"

    def test_ws_queued_control_key(self):
        assert C.WS_QUEUED_CONTROL_KEY == "queued_control"

    def test_ws_depth_levels_key(self):
        assert C.WS_DEPTH_LEVELS_KEY == "depth_levels"

    def test_ws_max_stream_instruments_key(self):
        assert C.WS_MAX_STREAM_INSTRUMENTS_KEY == "max_stream_instruments"

    def test_ws_feed_state_key(self):
        assert C.WS_FEED_STATE_KEY == "feed_state"

    def test_ws_feed_ready_key(self):
        assert C.WS_FEED_READY_KEY == "feed_ready"

    def test_ws_instruments_key(self):
        assert C.WS_INSTRUMENTS_KEY == "instruments"

    def test_ws_depth_key(self):
        assert C.WS_DEPTH_KEY == "depth"

    def test_ws_message_key(self):
        assert C.WS_MESSAGE_KEY == "message"

    def test_ws_total_bars_key(self):
        assert C.WS_TOTAL_BARS_KEY == "total_bars"

    def test_ws_simulated_key(self):
        assert C.WS_SIMULATED_KEY == "simulated"

    def test_ws_wall_seconds_per_bar_key(self):
        assert C.WS_WALL_SECONDS_PER_BAR_KEY == "wall_seconds_per_bar"

    def test_ws_speed_key(self):
        assert C.WS_SPEED_KEY == "speed"

    def test_ws_bar_index_key(self):
        assert C.WS_BAR_INDEX_KEY == "bar_index"

    def test_ws_index_key(self):
        assert C.WS_INDEX_KEY == "index"

    def test_ws_target_index_key(self):
        assert C.WS_TARGET_INDEX_KEY == "target_index"

    def test_ws_start_time_key(self):
        assert C.WS_START_TIME_KEY == "start_time"

    def test_ws_seed_key(self):
        assert C.WS_SEED_KEY == "seed"

    def test_ws_ticks_per_bar_key(self):
        assert C.WS_TICKS_PER_BAR_KEY == "ticks_per_bar"

    def test_ws_minutes_key(self):
        assert C.WS_MINUTES_KEY == "minutes"

    def test_ws_method_key(self):
        assert C.WS_METHOD_KEY == "method"

    def test_ws_paused_key(self):
        assert C.WS_PAUSED_KEY == "paused"

    def test_ws_snapshot_key(self):
        assert C.WS_SNAPSHOT_KEY == "snapshot"

    def test_ws_bars_key(self):
        assert C.WS_BARS_KEY == "bars"

    def test_ws_indicators_key(self):
        assert C.WS_INDICATORS_KEY == "indicators"

    def test_ws_ids_key(self):
        assert C.WS_IDS_KEY == "ids"

    def test_ws_mode_key(self):
        assert C.WS_MODE_KEY == "mode"

    def test_ws_last_closed_key(self):
        assert C.WS_LAST_CLOSED_KEY == "lastClosed"

    def test_ws_current_bucket_key(self):
        assert C.WS_CURRENT_BUCKET_KEY == "currentBucket"


class TestWorkspaceConstants:
    def test_workspace_data_key(self):
        assert C.WORKSPACE_DATA_KEY == "data"

    def test_workspace_revision_key(self):
        assert C.WORKSPACE_REVISION_KEY == "revision"

    def test_workspace_layout_id_key(self):
        assert C.WORKSPACE_LAYOUT_ID_KEY == "layout_id"

    def test_workspace_updated_at_key(self):
        assert C.WORKSPACE_UPDATED_AT_KEY == "updated_at"


class TestAuthConstants:
    def test_auth_authenticated_key(self):
        assert C.AUTH_AUTHENTICATED_KEY == "authenticated"

    def test_auth_session_auth_available_key(self):
        assert C.AUTH_SESSION_AUTH_AVAILABLE_KEY == "session_auth_available"

    def test_auth_subject_key(self):
        assert C.AUTH_SUBJECT_KEY == "subject"

    def test_auth_csrf_token_key(self):
        assert C.AUTH_CSRF_TOKEN_KEY == "csrf_token"

    def test_logged_out_key(self):
        assert C.LOGGED_OUT_KEY == "logged_out"

    def test_password_key(self):
        assert C.PASSWORD_KEY == "password"

    def test_order_id_key(self):
        assert C.ORDER_ID_KEY == "order_id"

    def test_modified_key(self):
        assert C.MODIFIED_KEY == "modified"

    def test_cancelled_key(self):
        assert C.CANCELLED_KEY == "cancelled"


class TestAPIResponseKeys:
    def test_unknown_strategy_key(self):
        assert C.UNKNOWN_STRATEGY_KEY == "unknown strategy"

    def test_unknown_params_key(self):
        assert C.UNKNOWN_PARAMS_KEY == "unknown params"

    def test_strategy_unavailable_key(self):
        assert C.STRATEGY_UNAVAILABLE_KEY == "strategy unavailable"

    def test_unsupported_interval_key(self):
        assert C.UNSUPPORTED_INTERVAL_KEY == "unsupported interval"

    def test_symbol_required_key(self):
        assert C.SYMBOL_REQUIRED_KEY == "'symbol' required"

    def test_no_datalake_data_key(self):
        assert C.NO_DATALAKE_DATA_KEY == "no datalake data for"

    def test_unknown_scanner_key(self):
        assert C.UNKNOWN_SCANNER_KEY == "unknown scanner"

    def test_window_days_integer_key(self):
        assert C.WINDOW_DAYS_INTEGER_KEY == "window_days must be an integer"

    def test_window_days_range_key(self):
        assert C.WINDOW_DAYS_RANGE_KEY == "window_days must be 5..120"

    def test_body_id_required_key(self):
        assert C.BODY_ID_REQUIRED_KEY == "body must include string 'id'"

    def test_params_object_key(self):
        assert C.PARAMS_OBJECT_KEY == "'params' must be an object"

    def test_transform_bars_required_key(self):
        assert C.TRANSFORM_BARS_REQUIRED_KEY == "transform requires a non-empty bars array"

    def test_profile_bars_required_key(self):
        assert C.PROFILE_BARS_REQUIRED_KEY == "profile requires a non-empty bars array"

    def test_body_object_key(self):
        assert C.BODY_OBJECT_KEY == "body must be an object"

    def test_from_utc_seconds_key(self):
        assert C.FROM_UTC_SECONDS_KEY == "'from' must be UTC seconds"

    def test_to_utc_seconds_key(self):
        assert C.TO_UTC_SECONDS_KEY == "'to' must be UTC seconds"

    def test_orderbook_unavailable_key(self):
        assert C.ORDERBOOK_UNAVAILABLE_KEY == "orderbook unavailable"

    def test_positions_unavailable_key(self):
        assert C.POSITIONS_UNAVAILABLE_KEY == "positions unavailable"

    def test_malformed_order_row_key(self):
        assert C.MALFORMED_ORDER_ROW_KEY == "malformed order row"

    def test_account_unavailable_key(self):
        assert C.ACCOUNT_UNAVAILABLE_KEY == "account unavailable"

    def test_no_workspace_key(self):
        assert C.NO_WORKSPACE_KEY == "no workspace"

    def test_stale_revision_key(self):
        assert C.STALE_REVISION_KEY == "stale revision"

    def test_layout_id_length_key(self):
        assert C.LAYOUT_ID_LENGTH_KEY == "layout_id must be 1..256 chars"

    def test_instruments_non_empty_key(self):
        assert C.INSTRUMENTS_NON_EMPTY_KEY == "instruments must be a non-empty list"

    def test_invalid_json_key(self):
        assert C.INVALID_JSON_KEY == "invalid JSON"

    def test_message_object_key(self):
        assert C.MESSAGE_OBJECT_KEY == "message must be an object"

    def test_unknown_message_type_key(self):
        assert C.UNKNOWN_MESSAGE_TYPE_KEY == "unknown message type"

    def test_subscribe_bars_format_key(self):
        assert C.SUBSCRIBE_BARS_FORMAT_KEY == "subscribe_bars needs 'bars': [{instrument, interval}]"

    def test_bad_bar_subscription_key(self):
        assert C.BAD_BAR_SUBSCRIPTION_KEY == "bad bar subscription"

    def test_indicator_sub_format_key(self):
        assert C.INDICATOR_SUB_FORMAT_KEY == "indicator-sub needs 'indicators': [{instrument, interval, ids, mode?}]"

    def test_indicator_unsub_format_key(self):
        assert C.INDICATOR_UNSUB_FORMAT_KEY == "indicator-unsub needs 'indicators': [{instrument, interval, ids?}]"

    def test_replay_start_instrument_key(self):
        assert C.REPLAY_START_INSTRUMENT_KEY == "replay_start needs 'instrument'"

    def test_replay_instrument_format_key(self):
        assert C.REPLAY_INSTRUMENT_FORMAT_KEY == "replay instrument must be EXCHANGE:SYMBOL"

    def test_unsupported_replay_interval_key(self):
        assert C.UNSUPPORTED_REPLAY_INTERVAL_KEY == "unsupported replay interval"

    def test_replay_method_key(self):
        assert C.REPLAY_METHOD_KEY == "method must be ohlc, anchored, or bridge"

    def test_replay_dataset_empty_key(self):
        assert C.REPLAY_DATASET_EMPTY_KEY == "replay dataset is empty"

    def test_replay_quote_timestamp_key(self):
        assert C.REPLAY_QUOTE_TIMESTAMP_KEY == "replay quote has no timestamp"

    def test_replay_seek_active_key(self):
        assert C.REPLAY_SEEK_ACTIVE_KEY == "replay seek needs an active replay"

    def test_replay_seek_restart_failed_key(self):
        assert C.REPLAY_SEEK_RESTART_FAILED_KEY == "replay seek restart failed"

    def test_order_stream_unavailable_key(self):
        assert C.ORDER_STREAM_UNAVAILABLE_KEY == "order stream unavailable"

    def test_control_queue_overflow_close_reason_key(self):
        assert C.CONTROL_QUEUE_OVERFLOW_CLOSE_REASON_KEY == "control queue overflow; reconnect to resync"

    def test_invalid_api_key_close_reason_key(self):
        assert C.INVALID_API_KEY_CLOSE_REASON_KEY == "invalid api key"

    def test_authentication_required_close_reason_key(self):
        assert C.AUTHENTICATION_REQUIRED_CLOSE_REASON_KEY == "authentication required"

    def test_session_auth_not_available_key(self):
        assert C.SESSION_AUTH_NOT_AVAILABLE_KEY == "Session auth not available"

    def test_no_login_credentials_key(self):
        assert C.NO_LOGIN_CREDENTIALS_KEY == "No login credentials configured"

    def test_invalid_credentials_key(self):
        assert C.INVALID_CREDENTIALS_KEY == "Invalid credentials"

    def test_invalid_json_body_key(self):
        assert C.INVALID_JSON_BODY_KEY == "Invalid JSON body"

    def test_rate_limit_exceeded_key(self):
        assert C.RATE_LIMIT_EXCEEDED_KEY == "rate limit exceeded; too many order mutations"

    def test_csrf_origin_mismatch_key(self):
        assert C.CSRF_ORIGIN_MISMATCH_KEY == "CSRF: origin mismatch"

    def test_csrf_referer_mismatch_key(self):
        assert C.CSRF_REFERER_MISMATCH_KEY == "CSRF: referer mismatch"

    def test_csrf_token_invalid_key(self):
        assert C.CSRF_TOKEN_INVALID_KEY == "CSRF token invalid or missing"

    def test_invalid_api_key_key(self):
        assert C.INVALID_API_KEY_KEY == "Invalid API key"

    def test_error_codes(self):
        assert C.ERROR_CODES == {
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

    def test_error_envelope_key(self):
        assert C.ERROR_ENVELOPE_KEY == "error"

    def test_error_detail_keys_key(self):
        assert C.ERROR_DETAIL_KEYS_KEY == {"code": "code", "message": "message"}


class TestTradeFields:
    def test_trade_fields(self):
        assert C.TRADE_FIELDS == {"record": "record", "rejected": "rejected", "stop": "stop", "target": "target"}

    def test_metric_keys(self):
        assert C.METRIC_KEYS == {
            "total_return": "total_return",
            "sharpe": "sharpe",
            "max_drawdown": "max_drawdown",
            "num_trades": "num_trades",
            "num_rejected": "num_rejected",
            "num_unfilled": "num_unfilled",
            "total_fees": "total_fees",
        }

    def test_equity_curve_key(self):
        assert C.EQUITY_CURVE_KEY == "equity_curve"

    def test_trades_key(self):
        assert C.TRADES_KEY == "trades"

    def test_metrics_key(self):
        assert C.METRICS_KEY == "metrics"

    def test_message_key(self):
        assert C.MESSAGE_KEY == "message"

    def test_table_key(self):
        assert C.TABLE_KEY == "table"

    def test_meta_key(self):
        assert C.META_KEY == "meta"

    def test_id_key(self):
        assert C.ID_KEY == "id"

    def test_points_key(self):
        assert C.POINTS_KEY == "points"

    def test_bars_key(self):
        assert C.BARS_KEY == "bars"

    def test_source_key(self):
        assert C.SOURCE_KEY == "source"

    def test_timeframe_resampled_key(self):
        assert C.TIMEFRAME_RESAMPLED_KEY == "timeframe_resampled"

    def test_last_closed_time_key(self):
        assert C.LAST_CLOSED_TIME_KEY == "last_closed_time"

    def test_symbol_key(self):
        assert C.SYMBOL_KEY == "symbol"

    def test_exchange_key(self):
        assert C.EXCHANGE_KEY == "exchange"

    def test_interval_key(self):
        assert C.INTERVAL_KEY == "interval"

    def test_cached_key(self):
        assert C.CACHED_KEY == "cached"

    def test_indicators_key(self):
        assert C.INDICATORS_KEY == "indicators"

    def test_capabilities_key(self):
        assert C.CAPABILITIES_KEY == "capabilities"

    def test_capability_names(self):
        assert C.CAPABILITY_NAMES == {
            "supports_super_order": "supports_super_order",
            "supports_forever_order": "supports_forever_order",
            "supports_slice_order": "supports_slice_order",
            "supports_edis": "supports_edis",
            "supports_kill_switch": "supports_kill_switch",
        }

    def test_account_fields(self):
        assert C.ACCOUNT_FIELDS == {"balance": "balance", "margin": "margin", "equity": "equity"}

    def test_holdings_fields(self):
        assert C.HOLDINGS_FIELDS == {"instrument_id": "instrument_id", "quantity": "quantity"}

    def test_position_fields(self):
        assert C.POSITION_FIELDS == {
            "avg_price": "avg_price",
            "realized_pnl": "realized_pnl",
            "unrealized_pnl": "unrealized_pnl",
        }

    def test_portfolio_keys(self):
        assert C.PORTFOLIO_KEYS == {"positions": "positions", "account": "account"}

    def test_book_keys(self):
        assert C.BOOK_KEYS == {"orders": "orders", "positions": "positions"}

    def test_order_fields(self):
        assert C.ORDER_FIELDS == {
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

    def test_net_qty_key(self):
        assert C.NET_QTY_KEY == "netQty"

    def test_avg_price_key(self):
        assert C.AVG_PRICE_KEY == "avg_price"

    def test_account_id_key(self):
        assert C.ACCOUNT_ID_KEY == "account_id"

    def test_balance_key(self):
        assert C.BALANCE_KEY == "balance"

    def test_margin_key(self):
        assert C.MARGIN_KEY == "margin"

    def test_unrealized_pnl_key(self):
        assert C.UNREALIZED_PNL_KEY == "unrealized_pnl"

    def test_strategies_key(self):
        assert C.STRATEGIES_KEY == "strategies"

    def test_scanners_key(self):
        assert C.SCANNERS_KEY == "scanners"

    def test_backtestable_key(self):
        assert C.BACKTESTABLE_KEY == "backtestable"

    def test_strategy_fields(self):
        assert C.STRATEGY_FIELDS == {"id": "id", "params": "params"}

    def test_param_fields(self):
        assert C.PARAM_FIELDS == {"name": "name", "type": "type", "default": "default"}

    def test_scanner_fields(self):
        assert C.SCANNER_FIELDS == {
            "universe_size": "universe_size",
            "conditions": "conditions",
            "rank_by": "rank_by",
            "limit": "limit",
        }

    def test_safety_keys(self):
        assert C.SAFETY_KEYS == {
            "feed": "feed",
            "unknown_orders": "unknown_orders",
            "reconciliation": "reconciliation",
            "strategies": "strategies",
            "boot_refused": "boot_refused",
            "reconciliation_checked": "reconciliation_checked",
            "drift": "drift",
        }

    def test_unknown_order_fields(self):
        assert C.UNKNOWN_ORDER_FIELDS == {
            "order_id": "order_id",
            "instrument": "instrument",
            "symbol": "symbol",
            "side": "side",
            "quantity": "quantity",
            "correlation_id": "correlation_id",
            "age_seconds": "age_seconds",
            "since": "since",
        }

    def test_reconciliation_fields(self):
        assert C.RECONCILIATION_FIELDS == {
            "last_checked_at": "last_checked_at",
            "totals_by_severity": "totals_by_severity",
            "refusing_severities": "refusing_severities",
            "refusing": "refusing",
        }

    def test_strategy_safety_fields(self):
        assert C.STRATEGY_SAFETY_FIELDS == {
            "strategy_id": "strategy_id",
            "version": "version",
            "lifecycle_state": "lifecycle_state",
            "promotion_status": "promotion_status",
            "report_id": "report_id",
            "promotion_failures": "promotion_failures",
        }

    def test_feed_status_fields(self):
        assert C.FEED_STATUS_FIELDS == {
            "state": "state",
            "ready": "ready",
            "generation": "generation",
            "last_event_at": "last_event_at",
            "age_seconds": "age_seconds",
        }

    def test_live_orders_enabled_key(self):
        assert C.LIVE_ORDERS_ENABLED_KEY == "live_orders_enabled"


class TestStrategyNames:
    def test_strategy_names(self):
        assert C.STRATEGY_NAMES == {
            "sma_cross": "sma_cross",
            "mean_reversion": "mean_reversion",
            "bracket_breakout": "bracket_breakout",
        }

    def test_strategy_param_names(self):
        assert C.STRATEGY_PARAM_NAMES == {
            "fast": "fast",
            "slow": "slow",
            "period": "period",
            "lookback": "lookback",
            "atr_period": "atr_period",
            "stop_atr": "stop_atr",
            "reward": "reward",
        }

    def test_strategy_param_types(self):
        assert C.STRATEGY_PARAM_TYPES == {"int": "int", "float": "float"}

    def test_strategy_defaults(self):
        assert C.STRATEGY_DEFAULTS == {
            "sma_cross": {"fast": 5, "slow": 20},
            "mean_reversion": {"period": 14, "lookback": 20},
            "bracket_breakout": {"atr_period": 14, "stop_atr": 1.5, "reward": 2.0},
        }

    def test_strategy_factories(self):
        assert C.STRATEGY_FACTORIES == {
            "sma_cross": "sma_cross.SmaCrossStrategy",
            "mean_reversion": "mean_reversion.MeanReversionStrategy",
            "bracket_breakout": "bracket_breakout.BracketBreakoutStrategy",
        }

    def test_profile_ids(self):
        assert C.PROFILE_IDS == {"seasonality": "seasonality"}

    def test_seasonality_params(self):
        assert C.SEASONALITY_PARAMS == {
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


class TestProductTypes:
    def test_product_by_wire(self):
        assert C.PRODUCT_BY_WIRE == {"MIS": "MIS", "CNC": "CNC", "NRML": "NRML", "MTF": "MTF"}

    def test_product_types(self):
        assert C.PRODUCT_TYPES == {"INTRADAY": "INTRADAY", "DELIVERY": "DELIVERY", "MARGIN": "MARGIN", "MTF": "MTF"}

    def test_order_types(self):
        assert C.ORDER_TYPES == {"MARKET": "MARKET", "LIMIT": "LIMIT", "STOP": "STOP", "STOP_LIMIT": "STOP_LIMIT"}

    def test_order_statuses(self):
        assert C.ORDER_STATUSES == {
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

    def test_chart_order_statuses(self):
        assert C.CHART_ORDER_STATUSES == {
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

    def test_chart_order_types(self):
        assert C.CHART_ORDER_TYPES == {"MARKET": "MARKET", "LIMIT": "LIMIT", "STOP": "SL", "STOP_LIMIT": "SL-M"}

    def test_unsettled_order_statuses(self):
        assert C.UNSETTLED_ORDER_STATUSES == {"NEW", "PENDING", "ACK", "SUBMITTED", "PARTIALLY_FILLED", "UNKNOWN"}


class TestHTTPStatusMap:
    def test_http_status_map(self):
        assert C.HTTP_STATUS_MAP == {
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
