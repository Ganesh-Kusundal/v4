"""Trace context: the correlation id as the id you grep (AC-17 / lane 5c).

The platform already had an identity that follows one order from the tick that
provoked it, through the strategy that turned that tick into a signal, into the
order, and out to the venue call: the domain ``CorrelationId``
(``tradex_domain.value_objects.CorrelationId``). The strategy engine derives it
deterministically from the triggering event, the engine requires it on every
mutation, and it is persisted in both the ``orders`` and ``idempotency`` tables.

So this module does **not** invent a second ``trace_id``. It carries the
existing ``CorrelationId`` — the same string an operator can look up in the
database — into the logging context, so every log line emitted while that order
is in flight is stamped with it. A second identifier would be one more thing to
correlate against the first, and the only thing it would add is a way for the
two to disagree.

Why it lives here and not in ``tradex_runtime.logging_config``, where the same
contextvar used to be defined: the two packages that must set the id on the
money path are ``tradex_execution`` (the submit pipeline) and
``tradex_runtime`` (the market feed), and the import-boundary graph in
``tests/test_import_boundaries.py`` lets ``execution`` import
``tradex_observability`` but **not** ``tradex_runtime``. This package is a leaf
— it imports nothing from the workspace — so both can reach it, and so can any
later package that needs to be traced, without a layering edge.
``tradex_runtime.logging_config`` re-exports this API unchanged, so the
established entry points and their tests keep working.
"""

from __future__ import annotations

import contextvars
import logging
from typing import Any

#: The trace id for the current context, or ``None`` when the current unit of
#: work has no order identity. Deliberately a plain ``str`` rather than a
#: ``CorrelationId``: this package is a leaf and may not import the domain, and
#: the value is only ever used as a log field.
_correlation_id: contextvars.ContextVar[str | None] = contextvars.ContextVar(
    "correlation_id", default=None,
)

#: The value used in output when there is no trace id. A record without an id is
#: rendered, never dropped and never given an invented one — a fabricated id is
#: worse than an absent one, because it is greppable and wrong.
NO_TRACE = "-"

#: Attributes every ``logging.LogRecord`` carries. Anything else on a record was
#: attached by the caller via ``extra=``, and is therefore a structured field
#: the caller meant to expose.
_RESERVED_RECORD_ATTRS = frozenset({
    "args", "asctime", "created", "exc_info", "exc_text", "filename",
    "funcName", "levelname", "levelno", "lineno", "message", "module",
    "msecs", "msg", "name", "pathname", "process", "processName",
    "relativeCreated", "stack_info", "taskName", "thread", "threadName",
})


def set_correlation_id(value: str | None) -> contextvars.Token[str | None]:
    """Set the trace id for the current context.

    Returns a token for resetting (pass it to :func:`reset_correlation_id`).
    """
    return _correlation_id.set(value)


def get_correlation_id() -> str | None:
    """Return the current trace id, or ``None``."""
    return _correlation_id.get()


def reset_correlation_id(token: contextvars.Token[str | None]) -> None:
    """Restore the trace id that :func:`set_correlation_id` displaced."""
    _correlation_id.reset(token)


def trace_id_of(correlation_id: Any) -> str | None:
    """Normalise anything correlation-id-shaped to a loggable id.

    Accepts a domain ``CorrelationId``, a raw string, or ``None`` — the call
    sites hold ``CorrelationId | None`` because the order request allows an
    absent id, and each of them would otherwise repeat the same ``str() if not
    None else None`` incantation.
    """
    if correlation_id is None:
        return None
    return str(correlation_id)


class CorrelationIdFilter(logging.Filter):
    """Stamp ``correlation_id`` onto every record from the context.

    A record that already carries one — attached by the call site through
    ``extra={"correlation_id": ...}`` — keeps it. The chain points that log
    before an order exists (the tick entering the feed, the signal leaving the
    strategy) attach the id explicitly precisely because nothing has set the
    contextvar yet, and a contextvar default would otherwise overwrite the
    value the caller went to the trouble of providing.
    """

    def filter(self, record: logging.LogRecord) -> bool:
        existing = getattr(record, "correlation_id", None)
        if not existing or existing == NO_TRACE:
            record.correlation_id = get_correlation_id() or NO_TRACE  # type: ignore[attr-defined]
        return True


def structured_fields(record: logging.LogRecord) -> dict[str, Any]:
    """The caller-attached fields on *record*, ready to be rendered as JSON.

    The stdlib has no accessor for ``extra``, so the record is scanned for
    attributes outside the reserved set. That is what makes ``extra={"stage":
    "venue.request", ...}`` a real JSON key in the output rather than text an
    operator has to grep for.
    """
    return {
        key: value
        for key, value in vars(record).items()
        if key not in _RESERVED_RECORD_ATTRS and not key.startswith("_")
    }


__all__ = [
    "NO_TRACE",
    "CorrelationIdFilter",
    "get_correlation_id",
    "reset_correlation_id",
    "set_correlation_id",
    "structured_fields",
    "trace_id_of",
]
