"""Standalone acceptance probes replaying the butterfly reviewer's 5 repro
scenarios against the current tree. Independent of the repo's own tests —
imports the production modules and asserts the reviewed defects are gone.

Run from trading/: python probe_review_fixes.py
"""
import threading
from datetime import UTC, datetime
from decimal import Decimal

from tradex_domain import Equity, OrderRequest, OrderSide, OrderType, Price, Quantity
from tradex_trading.events.data_source import BrokerOrderUpdate
from tradex_trading.events.risk_engine import RiskConfig
from tradex_trading.events.session import (
    BrokerConfig,
    DataSourceConfig,
    SessionConfig,
    TradingSession,
)

PASS = []
FAIL = []


def db_path(tag):
    import tempfile

    return tempfile.mkdtemp(prefix=f"probe-{tag}-") + "/events.db"


def request(corr, qty="10", price="2500"):
    return OrderRequest(
        instrument=Equity.of("NSE", "RELIANCE"),
        side=OrderSide.BUY,
        order_type=OrderType.LIMIT,
        quantity=Quantity(Decimal(qty)),
        price=Price(Decimal(price)),
        correlation_id=corr,
    )


def config(session_id, mode, tmp_db, ds_type, max_per_min=100, broker=None,
            historical_path=None):
    return SessionConfig(
        session_id=session_id,
        mode=mode,
        event_store_path=tmp_db,
        risk_config=RiskConfig(
            max_order_value=1_000_000.0,
            max_position_value=5_000_000.0,
            max_orders_per_minute=max_per_min,
            max_daily_loss=50_000.0,
        ),
        data_source=DataSourceConfig(
            type=ds_type,
            broker=broker,
            historical_path=historical_path,
        ),
    )


def probe1_live_fills_update_read_models():
    """Reviewer finding 1 (Critical): broker fills bypassed session projectors.
    Repro: live session, place, feed fill via fill_matcher → read models stuck
    at ACK / empty positions without restart.
    """
    db = db_path("live")
    cfg = config(
        "probe-live-001", "live", db, "broker",
        broker=BrokerConfig("dhan", "probe-client", "probe-token"),
    )
    session = TradingSession(cfg)
    session.start()

    place = session.place_order(request("probe-live-001"))
    assert place.success, place.error
    order_id = place.events[0].payload["order_id"]

    session.fill_matcher.process_update(
        BrokerOrderUpdate(
            broker_order_id=f"broker-{order_id[:8]}",
            instrument="NSE:RELIANCE",
            side="BUY",
            quantity=10,
            filled_quantity=10,
            fill_price=Decimal("2500"),
            status="FILLED",
            timestamp=datetime.now(UTC),
        )
    )

    order = session.get_orders()[0]
    positions = session.get_positions()
    ok = (
        order.status == "FILLED"
        and order.filled_quantity == Decimal("10")
        and len(positions) == 1
        and positions[0].quantity == Decimal("10")
    )
    detail = f"status={order.status} filled={order.filled_quantity} pos={[str(p.quantity) for p in positions]}"
    session.stop()
    return ok, detail


def probe2_duplicate_retry_returns_cached_result():
    """Reviewer finding 2 (Important): risk gate rejected a duplicate retry.
    Repro: max=2, place A, B, C → then retry A → wrongly rejected.
    """
    db = db_path("retry")
    cfg = config("probe-retry-001", "paper", db, "simulated", max_per_min=2)
    session = TradingSession(cfg)
    session.start()

    a = session.place_order(request("A"))
    b = session.place_order(request("B"))
    assert a.success and b.success, (a.error, b.error)

    # Fill the window completely (reviewer's exact repro): a third order
    # exhausts max_orders_per_minute=2, so a fresh risk check for A would
    # now fail. The retry must still return A's cached SUCCESS.
    session.place_order(request("C"))
    retry = session.place_order(request("A"))
    ok = (
        retry.success
        and retry.is_duplicate
        and "Risk check failed" not in (retry.error or "")
    )
    detail = f"retry.success={retry.success} is_duplicate={retry.is_duplicate} error={retry.error} orders={len(session.get_orders())}"
    session.stop()
    return ok, detail


def probe3_concurrent_duplicate_and_fill_races():
    """Reviewer finding 3 (Important): unsynchronized check-then-cache and
    fill-delta races. 8-thread same-correlation place; 2-thread same fill.
    """
    db = db_path("race")
    cfg = config("probe-race-001", "paper", db, "simulated", max_per_min=100)
    session = TradingSession(cfg)
    session.start()

    req = request("probe-race-same")
    barrier = threading.Barrier(8)
    results = []
    lock = threading.Lock()

    def worker():
        barrier.wait()
        r = session.place_order(req)
        with lock:
            results.append(r)

    threads = [threading.Thread(target=worker) for _ in range(8)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()

    executed = sum(1 for r in results if r.success and not r.is_duplicate)
    cached = sum(1 for r in results if r.is_duplicate)
    orders = session.get_orders()
    positions = session.get_positions()
    ok = executed == 1 and cached == 7 and len(orders) == 1 and len(positions) == 1 and positions[0].quantity == Decimal("10")
    detail = f"executed={executed} cached={cached} orders={len(orders)} pos_qty={positions[0].quantity if positions else None}"
    session.stop()

    # Fill race: backtest mode (no auto-fill), 2 threads same cumulative fill
    db2 = db_path("fillrace")
    cfg2 = config("probe-fillrace-001", "backtest", db2, "historical",
                  max_per_min=100, historical_path="data/ohlcv/")
    session2 = TradingSession(cfg2)
    session2.start()
    place = session2.place_order(request("probe-fillrace"))
    assert place.success, place.error
    order_id = place.events[0].payload["order_id"]

    barrier2 = threading.Barrier(2)

    def filler():
        barrier2.wait()
        session2.apply_fill(order_id, Decimal("10"), Decimal("2500"), fill_id="f1")

    threads = [threading.Thread(target=filler) for _ in range(2)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()

    events = session2._store.read_all("probe-fillrace-001")
    filled = [e for e in events if e.type == "OrderFilled" and e.payload["order_id"] == order_id]
    positions2 = session2.get_positions()
    ok2 = len(filled) == 1 and positions2[0].quantity == Decimal("10")
    detail2 = f"OrderFilled events={len(filled)} pos_qty={positions2[0].quantity if positions2 else None}"
    session2.stop()
    return ok and ok2, f"[dup] {detail} | [fill] {detail2}"


def probe4_kill_switch_read_models_and_doc():
    """Reviewer finding 4 (Important): kill switch local-only. Verify the
    limitation is documented (docstring mentions broker caveat).
    """
    db = db_path("kill")
    # backtest mode: order stays open (ACK) so the kill switch has something
    # to cancel (paper mode auto-fills instantly -> terminal FILLED, nothing
    # open to cancel).
    cfg = config("probe-kill-001", "backtest", db, "historical",
                 max_per_min=100, historical_path="data/ohlcv/")
    session = TradingSession(cfg)
    session.start()

    place = session.place_order(request("probe-kill-001"))
    assert place.success, place.error

    kill = session.trip_kill_switch("probe halt")
    events_types = {e.type for e in kill.events}
    order_after = session.get_orders()[0]
    ok = (
        kill.success
        and "KillSwitchTripped" in events_types
        and "OrderCancelled" in events_types
        and order_after.status == "CANCELLED"
        and session.place_order(request("probe-kill-002")).success is False
    )
    detail = f"kill.success={kill.success} types={events_types} order_status={order_after.status} post_kill_place_blocked=True"
    # docstring caveat present?
    doc = TradingSession.trip_kill_switch.__doc__ or ""
    has_doc = "broker" in doc.lower()
    session.stop()
    return ok and has_doc, f"{detail} | doc_caveat={has_doc}"


def probe5_rate_limit_boundary():
    """Reviewer finding 5 (Important): off-by-one admitted max+1.
    Repro: max=2 → 3 same-minute orders all passed. Now the 3rd must reject.
    """
    db = db_path("rate")
    cfg = config("probe-rate-001", "paper", db, "simulated", max_per_min=2)
    session = TradingSession(cfg)
    session.start()

    a = session.place_order(request("A"))
    b = session.place_order(request("B"))
    c = session.place_order(request("C"))
    ok = a.success and b.success and not c.success and "Rate limit" in c.error and len(session.get_orders()) == 2
    detail = f"A={a.success} B={b.success} C={c.success} err={c.error!r} orders={len(session.get_orders())}"
    session.stop()
    return ok, detail


PROBES = [
    ("1 live fills update read models (C1)", probe1_live_fills_update_read_models),
    ("2 duplicate retry cached (I2)", probe2_duplicate_retry_returns_cached_result),
    ("3 concurrent races (I3)", probe3_concurrent_duplicate_and_fill_races),
    ("4 kill switch (I4)", probe4_kill_switch_read_models_and_doc),
    ("5 rate limit boundary (I5)", probe5_rate_limit_boundary),
]


def main():
    import shutil
    import tempfile

    bases = []
    for name, fn in PROBES:
        # each probe creates its own tmp dirs via db_path; keep bases for cleanup
        try:
            ok, detail = fn()
            status = "PASS" if ok else "FAIL"
            if not ok:
                FAIL.append(name)
            else:
                PASS.append(name)
            print(f"[{status}] {name}: {detail}")
        except Exception as exc:
            FAIL.append(name)
            print(f"[ERROR] {name}: {type(exc).__name__}: {exc}")
            raise
    print()
    print(f"probes passed: {len(PASS)}/{len(PASS) + len(FAIL)}")
    return 0 if not FAIL else 1


if __name__ == "__main__":
    raise SystemExit(main())
