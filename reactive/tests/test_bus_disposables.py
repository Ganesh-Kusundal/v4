"""Regression tests for ReactiveBus tracked disposables."""

from __future__ import annotations

from tradex_domain.market import Quote
from tradex_reactive.bus import ReactiveBus


def test_of_type_subscription_dispose_is_safe_and_idempotent() -> None:
    bus = ReactiveBus()
    try:
        received: list[Quote] = []
        disposable = bus.of_type(Quote).subscribe(received.append)
        assert disposable.is_disposed is False
        disposable.dispose()
        assert disposable.is_disposed is True
        disposable.dispose()
    finally:
        bus.dispose()


def test_dispose_removes_the_tracked_subscription() -> None:
    bus = ReactiveBus()
    disposable = bus.of_type(Quote).subscribe(lambda _quote: None)
    disposable.dispose()
    assert bus._safe_disposables == set()  # noqa: SLF001
    bus.dispose()
