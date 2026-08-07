"""Tests for v3→v4 runtime and config port (Tasks 1-7)."""

from __future__ import annotations

import json
import os
from dataclasses import dataclass
from decimal import Decimal
from pathlib import Path
from unittest.mock import MagicMock

import pytest
from tradex_brokers.dhan.master import parse_dhan_master
from tradex_brokers.upstox.master import parse_upstox_master

from tradex_trading.config.env import _parse_bool
from tradex_trading.config.loader import load_config
from tradex_trading.config.schema import (
    AppConfig,
    BrokerConfig,
    LoggingConfig,
    ObservabilityConfig,
    PersistenceConfig,
    RiskConfig,
    _build,
)
from tradex_trading.runtime.health import (
    AggregateHealthCheck,
    CacheHealthCheck,
    ClockHealthCheck,
    ComponentHealth,
    ComponentState,
    MessageBusHealthCheck,
)
from tradex_trading.runtime.live import (
    _curl_cffi_available,
    _env,
    _env_with_deprecated_fallback,
    load_env_file,
    provider_environment,
)
from tradex_trading.runtime.metrics import MetricsRegistry, _Counter, _Gauge, _Histogram
from tradex_trading.runtime.startup import RuntimeContext, _broker_matches_config

# ---------------------------------------------------------------------------
# Task 2: config/schema.py
# ---------------------------------------------------------------------------


class TestSchemaPorts:
    def test_logging_config_defaults(self) -> None:
        cfg = LoggingConfig()
        assert cfg.level == "INFO"

    def test_persistence_config_defaults(self) -> None:
        cfg = PersistenceConfig()
        assert cfg.path is None

    def test_broker_config_defaults(self) -> None:
        cfg = BrokerConfig()
        assert cfg.name == "paper"
        assert cfg.environment == "PAPER"

    def test_observability_config_defaults(self) -> None:
        cfg = ObservabilityConfig()
        assert cfg.enabled is True

    def test_risk_config_post_init_converts_notional(self) -> None:
        cfg = RiskConfig(max_order_notional=100.5)
        assert isinstance(cfg.max_order_notional, Decimal)
        assert cfg.max_order_notional == Decimal("100.5")

    def test_risk_config_post_init_none(self) -> None:
        cfg = RiskConfig()
        assert cfg.max_order_notional is None

    def test_app_config_from_dict_minimal(self) -> None:
        cfg = AppConfig.from_dict({})
        assert cfg.mode == "paper"
        assert cfg.live_enabled is False
        assert cfg.broker.name == "paper"

    def test_app_config_from_dict_full(self) -> None:
        data = {
            "mode": "live",
            "live_enabled": True,
            "environment": "LIVE",
            "broker": {"name": "dhan", "environment": "LIVE"},
            "risk": {"max_order_notional": 50000},
            "logging": {"level": "DEBUG"},
            "observability": {"enabled": False},
            "persistence": {"path": "/tmp/test.db"},
        }
        cfg = AppConfig.from_dict(data)
        assert cfg.mode == "live"
        assert cfg.live_enabled is True
        assert cfg.broker.name == "dhan"
        assert cfg.risk.max_order_notional == Decimal("50000")
        assert cfg.logging.level == "DEBUG"
        assert cfg.observability.enabled is False
        assert cfg.persistence.path == "/tmp/test.db"

    def test_app_config_from_dict_rejects_unknown(self) -> None:
        with pytest.raises(ValueError, match="unknown config sections"):
            AppConfig.from_dict({"nonexistent_section": True})

    def test_build_rejects_non_dict(self) -> None:
        with pytest.raises(ValueError, match="must be an object"):
            _build(BrokerConfig, "not a dict")

    def test_build_rejects_unknown_keys(self) -> None:
        with pytest.raises(ValueError, match="unknown BrokerConfig keys"):
            _build(BrokerConfig, {"name": "dhan", "bad_key": True})

    def test_build_returns_default_for_none(self) -> None:
        cfg = _build(LoggingConfig, None)
        assert cfg.level == "INFO"


# ---------------------------------------------------------------------------
# Task 3: config/loader.py
# ---------------------------------------------------------------------------


class TestLoaderPorts:
    def test_load_config_json(self, tmp_path: Path) -> None:
        config_file = tmp_path / "config.json"
        config_file.write_text(json.dumps({"mode": "backtest", "log_level": "DEBUG"}))
        cfg = load_config(config_file)
        assert cfg.mode == "backtest"
        assert cfg.log_level == "DEBUG"

    def test_load_config_not_found(self, tmp_path: Path) -> None:
        with pytest.raises(FileNotFoundError):
            load_config(tmp_path / "missing.json")

    def test_load_config_invalid_json(self, tmp_path: Path) -> None:
        config_file = tmp_path / "bad.json"
        config_file.write_text("{invalid json")
        with pytest.raises(ValueError, match="invalid JSON"):
            load_config(config_file)

    def test_load_config_non_object_json(self, tmp_path: Path) -> None:
        config_file = tmp_path / "array.json"
        config_file.write_text("[1, 2, 3]")
        with pytest.raises(ValueError, match="config root must be an object"):
            load_config(config_file)


# ---------------------------------------------------------------------------
# Task 4: config/env.py
# ---------------------------------------------------------------------------


class TestEnvPorts:
    def test_parse_bool_true_values(self) -> None:
        for v in ("1", "true", "True", "TRUE", "yes", "on", "  true  "):
            assert _parse_bool(v) is True, f"expected True for {v!r}"

    def test_parse_bool_false_values(self) -> None:
        for v in ("0", "false", "no", "off", "", "anything"):
            assert _parse_bool(v) is False, f"expected False for {v!r}"


# ---------------------------------------------------------------------------
# Task 5: runtime/startup.py
# ---------------------------------------------------------------------------


class TestStartupPorts:
    def test_broker_matches_config_paper(self) -> None:
        from tradex_brokers import PaperBroker

        config = AppConfig(broker=BrokerConfig(name="paper"))
        broker = PaperBroker()
        assert _broker_matches_config(config, broker) is True

    def test_broker_matches_config_mismatch(self) -> None:
        from tradex_brokers import DhanBroker

        config = AppConfig(broker=BrokerConfig(name="upstox"))
        broker = DhanBroker()
        assert _broker_matches_config(config, broker) is False

    def test_broker_matches_config_custom_with_provider(self) -> None:
        @dataclass
        class CustomBroker:
            provider: str = "dhan"

        config = AppConfig(broker=BrokerConfig(name="dhan"))
        assert _broker_matches_config(config, CustomBroker()) is True

    def test_runtime_context_is_dataclass(self) -> None:
        # Just verify the class has the expected fields.
        fields = {f.name for f in RuntimeContext.__dataclass_fields__.values()}
        assert "config" in fields
        assert "session" in fields
        assert "engine" in fields
        assert "bus" in fields
        assert "broker" in fields


# ---------------------------------------------------------------------------
# Task 6: runtime/health.py
# ---------------------------------------------------------------------------


class TestHealthPorts:
    def test_component_state_values(self) -> None:
        assert ComponentState.RUNNING == "RUNNING"
        assert ComponentState.DEGRADED == "DEGRADED"
        assert ComponentState.ERROR == "ERROR"
        assert ComponentState.UNKNOWN == "UNKNOWN"

    def test_component_health_defaults(self) -> None:
        h = ComponentHealth(component_id="test")
        assert h.state == ComponentState.RUNNING
        assert h.details == {}

    def test_message_bus_health_check_running(self) -> None:
        bus = MagicMock()
        bus.stopped = False
        check = MessageBusHealthCheck(bus)
        result = check.check()
        assert result.state == ComponentState.RUNNING

    def test_message_bus_health_check_error(self) -> None:
        bus = MagicMock()
        bus.stopped = True
        check = MessageBusHealthCheck(bus)
        result = check.check()
        assert result.state == ComponentState.ERROR

    def test_cache_health_check(self) -> None:
        cache = MagicMock()
        cache.snapshot.return_value = {"orders": {1: "a"}, "positions": {}}
        check = CacheHealthCheck(cache)
        result = check.check()
        assert result.state == ComponentState.RUNNING
        assert result.details["orders"] == 1
        assert result.details["positions"] == 0

    def test_clock_health_check_ok(self) -> None:
        clock = MagicMock()
        clock.now.return_value = 12345.0
        check = ClockHealthCheck(clock)
        result = check.check()
        assert result.state == ComponentState.RUNNING

    def test_clock_health_check_error(self) -> None:
        clock = MagicMock()
        clock.now.side_effect = RuntimeError("clock broken")
        check = ClockHealthCheck(clock)
        result = check.check()
        assert result.state == ComponentState.ERROR
        assert "clock broken" in result.details["error"]

    def test_aggregate_health_check_worst_state(self) -> None:
        ok = MagicMock()
        ok.check.return_value = ComponentHealth("a", ComponentState.RUNNING)
        bad = MagicMock()
        bad.check.return_value = ComponentHealth("b", ComponentState.ERROR)
        agg = AggregateHealthCheck([ok, bad])
        result = agg.check()
        assert result.state == ComponentState.ERROR
        assert agg.is_ready() is False

    def test_aggregate_health_check_empty(self) -> None:
        agg = AggregateHealthCheck([])
        assert agg.is_ready() is True


# ---------------------------------------------------------------------------
# Task 7: runtime/metrics.py
# ---------------------------------------------------------------------------


class TestMetricsPorts:
    def test_counter_inc(self) -> None:
        c = _Counter("test")
        c.inc()
        c.inc(4)
        assert c.value() == 5.0

    def test_gauge_set(self) -> None:
        g = _Gauge("test")
        g.set(42.0)
        assert g.value() == 42.0

    def test_histogram_observe(self) -> None:
        h = _Histogram("test")
        h.observe(1.5)
        h.observe(2.5)
        assert h.value() == 4.0

    def test_registry_counter(self) -> None:
        reg = MetricsRegistry()
        c = reg.counter("orders")
        c.inc()
        assert reg.counter("orders").value() == 1.0

    def test_registry_gauge(self) -> None:
        reg = MetricsRegistry()
        g = reg.gauge("positions")
        g.set(10)
        assert reg.gauge("positions").value() == 10.0

    def test_registry_histogram(self) -> None:
        reg = MetricsRegistry()
        h = reg.histogram("latency")
        h.observe(0.5)
        assert reg.histogram("latency").value() == 0.5

    def test_registry_snapshot(self) -> None:
        reg = MetricsRegistry()
        reg.counter("a").inc(1)
        reg.gauge("b").set(2)
        reg.histogram("c").observe(3)
        snap = reg.snapshot()
        assert snap["a"] == 1.0
        assert snap["b"] == 2.0
        assert snap["c"] == 3.0

    def test_registry_get_unknown_returns_zero(self) -> None:
        reg = MetricsRegistry()
        assert reg.get("nonexistent") == 0

    def test_registry_reset(self) -> None:
        reg = MetricsRegistry()
        reg.counter("a").inc()
        reg.reset()
        assert reg.snapshot() == {}


# ---------------------------------------------------------------------------
# Task 1: runtime/live.py (selected pure-function tests)
# ---------------------------------------------------------------------------


class TestLivePorts:
    def test_provider_environment_default(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.delenv("DHAN_ENVIRONMENT", raising=False)
        assert provider_environment("DHAN") == "LIVE"

    def test_provider_environment_set(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setenv("UPSTOX_ENVIRONMENT", "sandbox")
        assert provider_environment("UPSTOX") == "SANDBOX"

    def test_env_required_missing(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.delenv("MISSING_VAR", raising=False)
        from tradex_domain import AuthenticationError

        with pytest.raises(AuthenticationError, match="missing live credential"):
            _env("MISSING_VAR", required=True)

    def test_env_optional_missing(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.delenv("MISSING_VAR", raising=False)
        assert _env("MISSING_VAR", required=False) == ""

    def test_env_with_deprecated_fallback_new(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setenv("NEW_VAR", "new_value")
        monkeypatch.delenv("OLD_VAR", raising=False)
        assert _env_with_deprecated_fallback("NEW_VAR", "OLD_VAR", "default") == "new_value"

    def test_env_with_deprecated_fallback_old(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.delenv("NEW_VAR", raising=False)
        monkeypatch.setenv("OLD_VAR", "old_value")
        with pytest.warns(DeprecationWarning, match="OLD_VAR is deprecated"):
            result = _env_with_deprecated_fallback("NEW_VAR", "OLD_VAR", "default")
        assert result == "old_value"

    def test_env_with_deprecated_fallback_default(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.delenv("NEW_VAR", raising=False)
        monkeypatch.delenv("OLD_VAR", raising=False)
        assert _env_with_deprecated_fallback("NEW_VAR", "OLD_VAR", "default") == "default"

    def test_load_env_file(self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
        env_file = tmp_path / ".env"
        env_file.write_text("TEST_VAR_1=hello\n# comment\nexport TEST_VAR_2=world\n")
        monkeypatch.delenv("TEST_VAR_1", raising=False)
        monkeypatch.delenv("TEST_VAR_2", raising=False)
        loaded = load_env_file(env_file)
        assert "TEST_VAR_1" in loaded
        assert "TEST_VAR_2" in loaded
        assert os.environ["TEST_VAR_1"] == "hello"
        assert os.environ["TEST_VAR_2"] == "world"

    def test_load_env_file_invalid(self, tmp_path: Path) -> None:
        env_file = tmp_path / ".env"
        env_file.write_text("no_equals_here")
        from tradex_domain import SDKError

        with pytest.raises(SDKError, match="invalid environment entry"):
            load_env_file(env_file)

    def test_parse_dhan_master_list(self) -> None:
        rows = [{"sym": "AAPL"}, {"sym": "GOOG"}]
        result = parse_dhan_master(rows, strict=False)
        assert len(result) == 2

    def test_parse_dhan_master_empty_bytes(self) -> None:
        assert parse_dhan_master(b"", strict=False) == []

    def test_parse_upstox_master_list(self) -> None:
        # Rows must carry an exchange + trading_symbol to survive normalization
        rows = [
            {"exchange": "NSE", "trading_symbol": "RELIANCE"},
            {"exchange": "NSE", "trading_symbol": "TCS"},
        ]
        result = parse_upstox_master(rows, strict=False)
        assert len(result) == 2

    def test_parse_upstox_master_empty_bytes(self) -> None:
        assert parse_upstox_master(b"", strict=False) == []

    def test_parse_dhan_master_emits_derivatives_structure(self) -> None:
        """MCX FUTCOM/OPTFUT rows carry instrument_type/right/expiry/strike/underlying."""
        csv_text = (
            "SEM_EXM_EXCH_ID,SEM_SEGMENT,SEM_TRADING_SYMBOL,SEM_SMST_SECURITY_ID,"
            "SEM_INSTRUMENT_NAME,SEM_OPTION_TYPE,SEM_EXPIRY_DATE,SEM_STRIKE_PRICE,"
            "SM_SYMBOL_NAME,SEM_CUSTOM_SYMBOL\n"
            "MCX,M,SILVER-04Sep2026-FUT,510000,FUTCOM,,2026-09-04 23:30:00,,SILVER,SILVER 04 SEP\n"
            "MCX,M,SILVERM-24Aug2026-279000-CE,509665,OPTFUT,CE,2026-08-24 23:30:00,"
            "279000.00000,SILVERM,SILVERM 24 AUG 279000 CALL\n"
        )
        rows = parse_dhan_master(csv_text.encode(), strict=False)
        fut = next(r for r in rows if r["instrument_type"] == "FUTCOM")
        assert fut["right"] is None
        assert fut["expiry"] == "2026-09-04"
        assert fut["underlying"] == "SILVER"
        opt = next(r for r in rows if r["instrument_type"] == "OPTFUT")
        assert opt["right"] == "CE"
        assert opt["expiry"] == "2026-08-24"
        assert opt["strike"] == "279000.00000"
        assert opt["underlying"] == "SILVERM"

    def test_parse_upstox_master_emits_derivatives_structure(self) -> None:
        """Upstox MCX rows carry instrument_type, ms-epoch expiry, strike, underlying."""
        rows = [
            {
                "exchange": "MCX",
                "trading_symbol": "GOLD 120000 CE 30 OCT 26",
                "instrument_key": "MCX_FO|579316",
                "instrument_type": "CE",
                "expiry": 1793384999000,
                "strike_price": 120000.0,
                "underlying_symbol": "GOLD",
                "segment": "MCX_FO",
            },
            {
                "exchange": "MCX",
                "trading_symbol": "GOLD 04SEP26 FUT",
                "instrument_key": "MCX_FO|559933",
                "instrument_type": "FUT",
                "expiry": 1793384999000,
                "underlying_symbol": "GOLD",
                "segment": "MCX_FO",
            },
        ]
        result = parse_upstox_master(rows, strict=False)
        assert len(result) == 2
        opt = next(r for r in result if r["right"] == "CE")
        assert opt["instrument_type"] == "CE"
        assert opt["expiry"] == "2026-10-30"
        assert opt["strike"] == 120000.0
        assert opt["underlying"] == "GOLD"
        fut = next(r for r in result if r["instrument_type"] == "FUT")
        assert fut["expiry"] == "2026-10-30"
        assert fut["underlying"] == "GOLD"

    def test_parse_upstox_master_maps_segment_to_canonical_exchange(self) -> None:
        """NSE_FO → NFO, NSE_COM → NSE_COMM, NSE_INDEX → IDX, etc."""
        rows = [
            {"exchange": "NSE", "trading_symbol": "MIDCPNIFTY 15225 PE 27 OCT 26",
             "segment": "NSE_FO", "instrument_type": "PE",
             "expiry": 1793384999000, "strike_price": 15225, "underlying_symbol": "MIDCPNIFTY"},
            {"exchange": "NSE", "trading_symbol": "SILVERM FUT 26 FEB 27",
             "segment": "NSE_COM", "instrument_type": "FUT",
             "expiry": 1803666599000, "underlying_symbol": "SILVERM"},
            {"exchange": "NSE", "trading_symbol": "NIFTY 50", "segment": "NSE_INDEX"},
            {"exchange": "BSE", "trading_symbol": "BANKNIFTY 30 DEC 26 PE",
             "segment": "BSE_FO", "instrument_type": "PE",
             "expiry": 1803666599000, "strike_price": 55000, "underlying_symbol": "BANKNIFTY"},
            {"exchange": "NSE", "trading_symbol": "GOLD 04 SEP 26 FUT",
             "segment": "NCD_FO", "instrument_type": "FUT",
             "expiry": 1793384999000, "underlying_symbol": "GOLD"},
        ]
        result = parse_upstox_master(rows, strict=False)
        by_symbol = {r["symbol"]: r for r in result}
        assert by_symbol["MIDCPNIFTY 15225 PE 27 OCT 26"]["exchange"] == "NFO"
        assert by_symbol["SILVERM FUT 26 FEB 27"]["exchange"] == "NSE_COMM"
        assert by_symbol["NIFTY 50"]["exchange"] == "IDX"
        assert by_symbol["BANKNIFTY 30 DEC 26 PE"]["exchange"] == "BFO"
        assert by_symbol["GOLD 04 SEP 26 FUT"]["exchange"] == "CDS"

    def test_curl_cffi_available_returns_bool(self) -> None:
        result = _curl_cffi_available()
        assert isinstance(result, bool)
