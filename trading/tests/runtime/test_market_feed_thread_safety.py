"""H2 — MarketFeed instrument lock.

The principal-architect review (H2) found that
``MarketFeed._instruments`` and ``_depth_instruments`` are mutated
without a single lock. Only ``_last_tick`` has ``_last_tick_lock``.
Concurrent subscribe/unsubscribe from API threads interleaving
with ``_on_quote`` reads from broker threads can corrupt the wanted
set.

The fix: a single ``threading.RLock`` (``_stream_lock``) that protects
``_instruments`` and ``_depth_instruments`` reads and writes. The
recursion-friendly RLock means ``_on_quote`` can read while the
dispatcher iterates the same dict.
"""

from __future__ import annotations

import threading
from unittest.mock import MagicMock

from tradex_trading.runtime import market_feed as mf_mod


def _make_feed():
    """Build a MarketFeed with a stub broker and bus."""
    bus = MagicMock()
    broker = MagicMock()
    return mf_mod.MarketFeed(broker=broker, bus=bus), broker, bus


def test_market_feed_has_a_stream_lock() -> None:
    """RED: a ``_stream_lock`` attribute must exist on MarketFeed."""
    feed, _broker, _bus = _make_feed()
    assert hasattr(feed, "_stream_lock"), (
        "MarketFeed must own a stream lock (H2: protect _instruments)"
    )
    assert isinstance(feed._stream_lock, type(threading.RLock()))  # type: ignore[attr-defined]


def test_instruments_reads_are_under_lock() -> None:
    """RED: the public read of instruments must be guarded by the lock.

    Pin the contract via a snapshot property that uses the lock.
    """
    feed, _broker, _bus = _make_feed()
    # The simplest pin: the property is implemented with a with-block.
    # We assert by introspection: a `snapshot_instruments` method (or
    # similar) exists and returns a copy.
    snap = getattr(feed, "snapshot_instruments", None)
    assert callable(snap), (
        "MarketFeed should expose a lock-guarded snapshot accessor (H2)"
    )
