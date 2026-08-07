"""Live smoke: Dhan MCX option chain -> WebSocket quote + depth streaming.

Proves the fixed MCX path end-to-end through the public SDK (no private
calls): the adapter previously gated MCX to the instrument master and
``_option_exchange`` built MCX legs as NFO contracts. Now:

  1. ``TradingSession.live(DHAN)`` boots READY (real creds, cached master).
  2. ``market.option_chain(CRUDEOIL)`` returns a LIVE REST chain with legs
     built on the MCX exchange (not NFO).
  3. ``market_feed.subscribe(ATM legs, depth="20")`` opens the Dhan WebSocket
     (quotes + depth-20 backend) for those MCX security ids.
  4. ``stream.subscribe_quotes/subscribe_depth`` deliver ticks on the bus.

Usage:  python smoke_mcx_stream.py [watch_seconds] [SYMBOL]
Default: 25s of CRUDEOIL. Swap SYMBOL (e.g. GOLD) via argv.
"""

from __future__ import annotations

import sys
import threading
import time
from collections import defaultdict
from pathlib import Path

ROOT = Path(__file__).resolve().parent
for sub in ("trading/src", "domain/src", "brokers/src"):
    sys.path.insert(0, str(ROOT / sub))

from tradex_domain import BrokerId  # noqa: E402
from tradex_domain.errors import CapabilityNotSupportedError  # noqa: E402
from tradex_domain.instruments import Equity  # noqa: E402
from tradex_domain.market import Depth, Quote  # noqa: E402
from tradex_trading.config.env import load_env_file  # noqa: E402
from tradex_trading.sdk.session import SessionState, TradingSession  # noqa: E402

load_env_file(ROOT / ".env.local", override=True)

SYMBOL = sys.argv[2].strip().upper() if len(sys.argv) > 2 else "CRUDEOIL"
WATCH_SECONDS = float(sys.argv[1]) if len(sys.argv) > 1 else 25.0

quotes: dict[str, list[Quote]] = defaultdict(list)
depths: dict[str, list[Depth]] = defaultdict(list)
_lock = threading.Lock()


def on_quote(q: Quote) -> None:
    with _lock:
        quotes[str(q.instrument.instrument_id)].append(q)


def on_depth(d: Depth) -> None:
    with _lock:
        depths[str(d.instrument.instrument_id)].append(d)


def main() -> int:
    session = TradingSession.live(BrokerId.DHAN, confirm=True)
    assert session.state is SessionState.READY, f"session not READY: {session.state}"
    try:
        # --- 1. Live MCX option chain (fixed REST path) ---------------------
        underlying = Equity.of("MCX", SYMBOL)
        chain = session.market.option_chain(underlying)
        expiries = chain.expiries()
        if not expiries:
            print(f"FAIL: no expiries for {SYMBOL} MCX")
            return 1
        near = expiries[0]
        pairs = list(near.pairs)
        spot = near.reference_price.value if near.reference_price is not None else None
        if spot is None:
            atm = pairs[len(pairs) // 2]
        else:
            atm = min(pairs, key=lambda p: abs(float(p.strike.value) - float(spot)))
        legs = [atm.call, atm.put]
        print(
            f"chain OK: {SYMBOL} MCX | spot ~{spot} | expiry {near.expiry_date} "
            f"| {len(pairs)} strikes"
        )
        for leg in legs:
            print(f"  ATM leg: {leg.instrument_id}")

        # --- 2. Subscribe the session bus + wire the WebSocket feed --------
        session.stream.subscribe_quotes(on_quote)
        session.stream.subscribe_depth(on_depth)
        # Depth is NSE-only platform-wide; MCX legs reject it loudly (no
        # silent empty book). Quotes still stream on MCX.
        try:
            session.market_feed.subscribe(legs, depth="20")
        except CapabilityNotSupportedError as exc:
            print(f"depth rejected loudly (expected — NSE only): {exc}")
        session.market_feed.subscribe(legs)
        print(f"feed subscribed: {len(legs)} MCX legs (quotes only)")

        deadline = time.monotonic() + WATCH_SECONDS
        while time.monotonic() < deadline:
            time.sleep(0.5)

        # --- 3. Report -------------------------------------------------------
        print(f"\n=== {WATCH_SECONDS:.0f}s of WebSocket ticks (MCX) ===")
        ok_quotes = ok_depth = False
        for leg in legs:
            key = str(leg.instrument_id)
            qs = quotes.get(key, [])
            ds = depths.get(key, [])
            if qs:
                ok_quotes = True
            if ds:
                ok_depth = True
            last_q = qs[-1] if qs else None
            last_d = ds[-1] if ds else None
            print(f"\n{leg.instrument_id}")
            if last_q is not None:
                print(
                    f"  quotes: {len(qs)} | last ltp={last_q.ltp.value} "
                    f"bid={last_q.bid.value if last_q.bid else '-'} "
                    f"ask={last_q.ask.value if last_q.ask else '-'} "
                    f"vol={last_q.volume} oi={last_q.open_interest}"
                )
            else:
                print("  quotes: 0")
            if last_d is not None:
                levels = getattr(last_d, "levels", None)
                print(
                    f"  depth: {len(ds)} | levels={levels} "
                    f"best_bid={last_d.bids[0][0].value if last_d.bids else '-'}x"
                    f"{last_d.bids[0][1].value if last_d.bids else 0} "
                    f"best_ask={last_d.asks[0][0].value if last_d.asks else '-'}x"
                    f"{last_d.asks[0][1].value if last_d.asks else 0} "
                    f"total_bids={len(last_d.bids)} asks={len(last_d.asks)}"
                )
            else:
                print("  depth: 0")

        # REST cross-check on a streamed leg.
        rest_quote = session.market.quote(atm.call)
        print(
            f"\nREST cross-check {atm.call.instrument_id}: ltp={rest_quote.ltp.value} "
            f"oi={rest_quote.open_interest}"
        )

        print()
        if ok_quotes and ok_depth:
            print("PASS: MCX quotes AND depth-20 stream over WebSocket")
            return 0
        if ok_quotes:
            print(
                "PARTIAL: MCX quotes stream; depth not subscribed (NSE only — "
                "depth requests on MCX raise CapabilityNotSupportedError)"
            )
            return 0
        print("FAIL: no MCX ticks received (market closed? socket/auth?)")
        return 1
    finally:
        try:
            session.stop()
        except Exception:  # noqa: BLE001 — teardown must not mask the result
            pass


if __name__ == "__main__":
    raise SystemExit(main())
