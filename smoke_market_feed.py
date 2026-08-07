"""Quick market-feed smoke test — standard TradingSession.live() interface.

Subscribes to one NSE equity (RELIANCE) via the session's MarketFeed and
waits up to 15 seconds for the first quote tick.

Usage:
    uv run python smoke_market_feed.py [broker]
    # broker: dhan (default) or upstox
"""

from __future__ import annotations

import os
import sys
import threading
import time
from pathlib import Path

# ---------------------------------------------------------------------------
# 0. Ensure project packages are importable
# ---------------------------------------------------------------------------
_root = Path(__file__).parent
for _sub in ("domain/src", "brokers/src", "trading/src"):
    sys.path.insert(0, str(_root / _sub))

# ---------------------------------------------------------------------------
# 1. Load .env.local before any broker import
# ---------------------------------------------------------------------------
env_file = _root / ".env.local"
if env_file.exists():
    for line in env_file.read_text().splitlines():
        line = line.strip()
        if line and not line.startswith("#") and "=" in line:
            k, v = line.split("=", 1)
            os.environ.setdefault(k.strip(), v.strip())

# ---------------------------------------------------------------------------
# 2. Imports (need rx on the path)
# ---------------------------------------------------------------------------
from tradex_domain import BrokerId  # noqa: E402
from tradex_domain.instruments import Equity  # noqa: E402
from tradex_trading.sdk.session import TradingSession  # noqa: E402

# ---------------------------------------------------------------------------
# 3. Globals
# ---------------------------------------------------------------------------
quotes_received: list = []
first_tick_event = threading.Event()


def on_quote(quote):
    quotes_received.append(quote)
    if len(quotes_received) == 1:
        first_tick_event.set()


# ---------------------------------------------------------------------------
# 4. Main
# ---------------------------------------------------------------------------
def main():
    broker_name = (sys.argv[1] if len(sys.argv) > 1 else "dhan").upper()
    broker_id = BrokerId(broker_name)

    print("=== Market Feed Smoke Test ===")
    print(f"Broker : {broker_name.upper()}")
    print("Symbol : RELIANCE (NSE EQUITY)")
    print()

    # Build a live session via the standard factory
    print("[1/4] Building live session ...")
    session = TradingSession.live(broker_id, confirm=True)
    print(f"      Session state: {session.state}")
    print(f"      Market feed bound: {session.market_feed is not None}")

    if session.market_feed is None:
        print("FAIL: no market feed bound to session")
        session.stop()
        return

    # Create the instrument
    reliance = Equity.of("NSE", "RELIANCE")
    print(f"[2/4] Instrument: {reliance.instrument_id}")

    # Subscribe via the bus (so we can observe ticks)
    print("[3/4] Subscribing to quote stream ...")
    bus = session.bus
    sub = bus.subscribe(on_quote)

    # Start the market feed
    print("[4/4] Starting market feed (waiting up to 15s for first tick) ...")
    t0 = time.monotonic()
    session.market_feed.start([reliance])
    print(f"      Feed active: {session.market_feed.active}")
    print(f"      Instruments on feed: {session.market_feed.instruments}")
    print(f"      Subscription count: {session.market_feed.subscription_count}")
    print()

    # Wait for first tick
    got_tick = first_tick_event.wait(timeout=15.0)
    elapsed = time.monotonic() - t0

    if got_tick:
        first = quotes_received[0]
        print(f"FIRST TICK received in {elapsed:.1f}s:")
        print(f"  Instrument : {first.instrument.instrument_id}")
        print(f"  LTP        : {first.ltp}")
        print(f"  Volume     : {first.volume}")
        print(f"  Timestamp  : {first.timestamp}")
        print()
        print(f"Total ticks in {elapsed:.1f}s: {len(quotes_received)}")
        print()
        print("RESULT: MARKET FEED WORKING")
    else:
        print(f"No ticks received after {elapsed:.1f}s")
        print(f"Feed active: {session.market_feed.active}")
        print(f"Subscriptions: {session.market_feed.subscription_count}")
        print()
        print("RESULT: MARKET FEED NOT RECEIVING TICKS")

    # Cleanup
    if hasattr(sub, 'dispose'):
        sub.dispose()
    session.stop()
    print()
    print("Session stopped.")


if __name__ == "__main__":
    main()
