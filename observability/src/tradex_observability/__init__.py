"""TradeX observability — metrics registry and trace context (stdlib-only)."""

from .metrics import (
    METRIC_ADOPTED,
    METRIC_CLEARED,
    METRIC_LAST_ADOPTED,
    METRIC_READ_FAILED,
    METRIC_REFUSED,
    METRIC_SKIPPED,
    MetricsRegistry,
)
from .tracing import (
    CorrelationIdFilter,
    get_correlation_id,
    reset_correlation_id,
    set_correlation_id,
    structured_fields,
    trace_id_of,
)

__all__ = [
    "CorrelationIdFilter",
    "METRIC_ADOPTED",
    "METRIC_CLEARED",
    "METRIC_LAST_ADOPTED",
    "METRIC_READ_FAILED",
    "METRIC_REFUSED",
    "METRIC_SKIPPED",
    "MetricsRegistry",
    "get_correlation_id",
    "reset_correlation_id",
    "set_correlation_id",
    "structured_fields",
    "trace_id_of",
]
