"""The fetch deadline must cross the fetcher boundary through a public seam.

`fetch_with_backoff` used to assign `fetcher._deadline` directly. That is a
foreign private write: it silently does nothing for a double that never reads
the attribute, and the deadline was lost in exactly those tests.
"""

from __future__ import annotations

from datetime import UTC, datetime
from types import SimpleNamespace

from tradex_domain.enums import Timeframe
from tradex_market_data.parallel_fetcher import fetch_with_backoff


class _RecordingFetcher:
    def __init__(self) -> None:
        self.deadlines: list[float | None] = []

    def set_deadline(self, deadline: float | None) -> None:
        self.deadlines.append(deadline)

    def fetch(self, instruments, timeframe, start, end, ranges=None):
        return {str(i.instrument_id): object() for i in instruments}, []


def test_fetch_with_backoff_sets_the_deadline_through_the_public_method() -> None:
    fetcher = _RecordingFetcher()
    batch = [SimpleNamespace(instrument_id="NSE:TEST")]
    stamp = datetime(2026, 10, 22, tzinfo=UTC)

    fetch_with_backoff(fetcher, batch, Timeframe.D1, stamp, stamp, deadline=123.5)

    assert fetcher.deadlines == [123.5]
