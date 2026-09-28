"""Structured logging with a correlation/trace id (PE-11, AC-17 lane 5c).

This is the operator-facing seam: it configures the root logger with a
formatter that includes timestamp, level, logger name, message, and the
correlation id, and it can emit JSON lines for ELK/Datadog. The correlation id
flows through a ``contextvars`` contextvar so concurrent requests on different
threads/tasks do not interleave.

The contextvar and the filter now live in ``tradex_observability.tracing`` so
the execution engine — which the import-boundary graph does not let reach
``tradex_runtime`` — can set the trace id on the money path. This module keeps
the historical import location working by re-exporting them, so
``tradex_runtime.logging_config`` remains the one name a caller imports.

``boot`` calls :func:`setup_structured_logging`; before AC-17 lane 5c it had no
production call site and the whole structured path was dead code.

Uses ONLY stdlib (logging, contextvars, json, datetime).
"""

from __future__ import annotations

import json
import logging
import sys
from datetime import UTC, datetime

# The trace context is a leaf module in ``tradex_observability`` (see the
# module docstring there for why it does not live in runtime). Re-exported here
# under the names this module has always exposed.
from tradex_observability.tracing import (
    NO_TRACE,
    CorrelationIdFilter,
    get_correlation_id,
    reset_correlation_id,
    set_correlation_id,
    structured_fields,
    trace_id_of,
)

__all__ = [
    "NO_TRACE",
    "CorrelationIdFilter",
    "JsonFormatter",
    "StructuredFormatter",
    "get_correlation_id",
    "reset_correlation_id",
    "set_correlation_id",
    "setup_structured_logging",
    "structured_fields",
    "trace_id_of",
]


# -- Formatters --------------------------------------------------------------

class StructuredFormatter(logging.Formatter):
    """Human-readable format with correlation_id bracket.

    ``getMessage`` is not enough here — the record's ``correlation_id`` is read
    by the format string, and a record that has not been through
    :class:`CorrelationIdFilter` has no such attribute. Rather than let the
    stdlib raise "Formatting field not found in record: 'correlation_id'" (which
    is a crash inside ``logging.emit``, not a diagnosable error), the missing
    field is defaulted to :data:`NO_TRACE` here. Rendering a record that has no
    id as ``-`` is correct; raising is not.
    """

    def __init__(self) -> None:
        super().__init__(
            fmt="%(asctime)s [%(levelname)-8s] %(name)s [%(correlation_id)s] %(message)s",
            datefmt="%Y-%m-%dT%H:%M:%S",
        )

    def format(self, record: logging.LogRecord) -> str:
        if not getattr(record, "correlation_id", None):
            record.correlation_id = NO_TRACE  # type: ignore[attr-defined]
        return super().format(record)

    def formatTime(self, record: logging.LogRecord, datefmt: str | None = None) -> str:
        return datetime.now(UTC).strftime(datefmt or self.datefmt)  # type: ignore[arg-type]


class JsonFormatter(logging.Formatter):
    """JSON-lines format for log aggregators (Datadog, ELK, etc.).

    ``correlation_id`` is always present — ``NO_TRACE`` when there is none — and
    any caller-attached ``extra`` fields are merged alongside it, so a chain
    point's ``stage``/``instrument``/``correlation_id`` are real JSON keys an
    operator can query rather than text they must parse.
    """

    def format(self, record: logging.LogRecord) -> str:
        log_entry: dict[str, object] = {
            "timestamp": datetime.now(UTC).isoformat(),
            "level": record.levelname,
            "logger": record.name,
            "message": record.getMessage(),
        }
        log_entry.update(structured_fields(record))
        # The filter normally stamps this, but a record formatted without the
        # filter (a directly-constructed one) must not KeyError.
        log_entry["correlation_id"] = getattr(record, "correlation_id", None) or NO_TRACE
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
