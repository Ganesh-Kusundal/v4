"""Typed stream operator helpers.

Convenience wrappers around RxPY operators for common trading-stream patterns.
"""

from __future__ import annotations

from collections.abc import Callable
from typing import Any

import rx
from rx import operators as ops


def of_type(msg_type: type):
    """Filter observable to only emit messages of the given type."""
    return ops.filter(lambda m, _t=msg_type: isinstance(m, _t))


def share():
    """Multicast observable to multiple subscribers."""
    return ops.share()


def replay_buffer(size: int = 1):
    """Replay last *size* items to new subscribers."""
    return ops.replay(buffer_size=size)


def throttle_first(period_ms: int):
    """Emit only the first item in each time window."""
    return ops.throttle_first(period_ms)


def sample(period_ms: int):
    """Sample the observable at regular intervals."""
    return ops.sample(period_ms)


def distinct_until_changed(key: Callable[[Any], Any] | None = None):
    """Suppress sequential duplicate emissions."""
    if key is not None:
        return ops.distinct_until_changed(key)
    return ops.distinct_until_changed()


def take_until(notifier: rx.Observable):
    """Take items until the *notifier* emits."""
    return ops.take_until(notifier)


def map_to(transform_fn: Callable[[Any], Any]):
    """Map each emission through a transform function."""
    return ops.map(transform_fn)


def filter_safe(predicate: Callable[[Any], bool]):
    """Filter with exception safety — errors become skips."""

    def safe_predicate(item: Any) -> bool:
        try:
            return predicate(item)
        except Exception:
            return False

    return ops.filter(safe_predicate)


def catch_error(handler: Callable[[Exception, Any], Any]):
    """Catch errors in the observable pipeline."""
    return ops.catch(handler)
