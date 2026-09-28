"""AC-22 test helper: record what the strategy engine hands to the venue.

These tests assert on the ``OrderRequest`` a strategy produces (size, price,
protective legs) rather than on a fill, so they do not need a real
``ExecutionEngine``. They hand the engine a recording callable instead — which
is exactly the seam AC-22 introduced, and is a *stronger* assertion than the
old ``bus.of_type(PlaceOrderCommand)`` subscription: it proves the request
reached the venue door, not merely that something was published where a test
was listening.
"""

from __future__ import annotations

from typing import Any


class RecordingSubmit:
    """Callable that records every ``OrderRequest`` handed to the venue.

    Returns a receipt-shaped object so a caller that inspects the return value
    does not crash; nothing in the strategy engine reads it.
    """

    def __init__(self) -> None:
        self.requests: list[Any] = []

    def __call__(self, request: Any) -> Any:
        self.requests.append(request)
        return None

    def __len__(self) -> int:
        return len(self.requests)

    def __getitem__(self, index: int) -> Any:
        return self.requests[index]
