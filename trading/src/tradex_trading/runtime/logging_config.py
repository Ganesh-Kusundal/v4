"""Structured logging with correlation IDs (PE-11).

Provides a lightweight structured logging seam:
- ``setup_structured_logging`` configures the root logger with a
  formatter that includes timestamp, level, logger name, message,
  and an optional correlation_id field.
- Correlation IDs flow through ``contextvars`` so concurrent requests
  on different threads/tasks don't interleave.
- Supports both human-readable and JSON-lines output.

Uses ONLY stdlib (logging, contextvars, json, datetime).
"""

from __future__ import annotations

import contextvars
import json
import logging
import sys
from datetime import UTC, datetime

# -- Correlation ID contextvar -----------------------------------------------

_correlation_id: contextvars.ContextVar[str | None] = contextvars.ContextVar(
    "correlation_id", default=None,
)


def set_correlation_id(value: str | None) -> contextvars.Token[str | None]:
    """Set the correlation ID for the current context. Returns a token
    for resetting (e.g. in a ``finally`` block)."""
    return _correlation_id.set(value)


def get_correlation_id() -> str | None:
    """Return the current correlation ID, or ``None``."""
    return _correlation_id.get()


# -- Logging filter ----------------------------------------------------------

class CorrelationIdFilter(logging.Filter):
    """Inject ``correlation_id`` into every log record from the contextvar."""

    def filter(self, record: logging.LogRecord) -> bool:
        record.correlation_id = get_correlation_id() or "-"  # type: ignore[attr-defined]
        return True


# -- Formatters --------------------------------------------------------------

class StructuredFormatter(logging.Formatter):
    """Human-readable format with correlation_id bracket."""

    def __init__(self) -> None:
        super().__init__(
            fmt="%(asctime)s [%(levelname)-8s] %(name)s [%(correlation_id)s] %(message)s",
            datefmt="%Y-%m-%dT%H:%M:%S",
        )

    def formatTime(self, record: logging.LogRecord, datefmt: str | None = None) -> str:
        return datetime.now(UTC).strftime(datefmt or self.datefmt)  # type: ignore[arg-type]


class JsonFormatter(logging.Formatter):
    """JSON-lines format for log aggregators (Datadog, ELK, etc.)."""

    def format(self, record: logging.LogRecord) -> str:
        log_entry = {
            "timestamp": datetime.now(UTC).isoformat(),
            "level": record.levelname,
            "logger": record.name,
            "message": record.getMessage(),
            "correlation_id": getattr(record, "correlation_id", "-"),
        }
        if record.exc_info and record.exc_info[0] is not None:
            log_entry["exception"] = self.formatException(record.exc_info)
        return json.dumps(log_entry, default=str)


# -- Setup -------------------------------------------------------------------

def setup_structured_logging(
    service_name: str,
    *,
    json_format: bool = False,
    level: int = logging.INFO,
) -> None:
    """Configure the root logger with structured formatting.

    Parameters
    ----------
    service_name : str
        Included in the logger name prefix (e.g. ``"tradex"``).
    json_format : bool
        When True, output JSON lines. When False, human-readable.
    level : int
        Root logger level (default: INFO).
    """
    root = logging.getLogger()
    root.setLevel(level)

    # Remove existing handlers to avoid duplicate output
    for handler in root.handlers[:]:
        root.removeHandler(handler)

    handler = logging.StreamHandler(sys.stderr)
    handler.setLevel(level)

    formatter: logging.Formatter
    if json_format:
        formatter = JsonFormatter()
    else:
        formatter = StructuredFormatter()

    handler.setFormatter(formatter)
    handler.addFilter(CorrelationIdFilter())
    root.addHandler(handler)
