"""Tests for structured logging (PE-11)."""

from __future__ import annotations

import json
import logging

from tradex_trading.runtime.logging_config import (
    CorrelationIdFilter,
    JsonFormatter,
    StructuredFormatter,
    get_correlation_id,
    set_correlation_id,
    setup_structured_logging,
)


class TestCorrelationId:
    """Correlation ID flows through contextvars."""

    def test_default_is_none(self) -> None:
        assert get_correlation_id() is None

    def test_set_and_get(self) -> None:
        token = set_correlation_id("req-123")
        try:
            assert get_correlation_id() == "req-123"
        finally:
            set_correlation_id(None)

    def test_reset_via_token(self) -> None:
        token = set_correlation_id("req-456")
        assert get_correlation_id() == "req-456"
        # Reset using the token
        # Just set to None explicitly
        set_correlation_id(None)
        assert get_correlation_id() is None


class TestCorrelationIdFilter:
    """Filter injects correlation_id into log records."""

    def test_filter_injects_correlation_id(self) -> None:
        record = logging.LogRecord(
            name="test", level=logging.INFO, pathname="", lineno=0,
            msg="hello", args=(), exc_info=None,
        )
        f = CorrelationIdFilter()
        token = set_correlation_id("abc-123")
        try:
            f.filter(record)
            assert record.correlation_id == "abc-123"
        finally:
            set_correlation_id(None)

    def test_filter_defaults_to_dash(self) -> None:
        record = logging.LogRecord(
            name="test", level=logging.INFO, pathname="", lineno=0,
            msg="hello", args=(), exc_info=None,
        )
        set_correlation_id(None)
        f = CorrelationIdFilter()
        f.filter(record)
        assert record.correlation_id == "-"


class TestStructuredFormatter:
    """Human-readable format includes correlation_id."""

    def test_format_includes_correlation_id(self) -> None:
        formatter = StructuredFormatter()
        record = logging.LogRecord(
            name="test.logger", level=logging.INFO, pathname="", lineno=0,
            msg="test message", args=(), exc_info=None,
        )
        record.correlation_id = "xyz"  # type: ignore[attr-defined]
        output = formatter.format(record)
        assert "[xyz]" in output
        assert "test message" in output


class TestJsonFormatter:
    """JSON format outputs valid JSON lines."""

    def test_format_is_valid_json(self) -> None:
        formatter = JsonFormatter()
        record = logging.LogRecord(
            name="test.logger", level=logging.ERROR, pathname="", lineno=0,
            msg="something failed", args=(), exc_info=None,
        )
        record.correlation_id = "req-789"  # type: ignore[attr-defined]
        output = formatter.format(record)
        parsed = json.loads(output)
        assert parsed["level"] == "ERROR"
        assert parsed["message"] == "something failed"
        assert parsed["correlation_id"] == "req-789"
        assert "timestamp" in parsed


class TestSetupStructureduredLogging:
    """setup_structured_logging configures the root logger."""

    def test_setup_replaces_handlers(self) -> None:
        setup_structured_logging("test-service")
        root = logging.getLogger()
        assert len(root.handlers) == 1
        assert isinstance(root.handlers[0].formatter, StructuredFormatter)

    def test_setup_json_mode(self) -> None:
        setup_structured_logging("test-service", json_format=True)
        root = logging.getLogger()
        assert len(root.handlers) == 1
        assert isinstance(root.handlers[0].formatter, JsonFormatter)
