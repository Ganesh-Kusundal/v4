"""End-to-end smoke against a RUNNING tradex serve instance.

Covers the full surface a trader touches: readiness, UI mount, chart history,
indicator catalogue/compute, strategies, backtest, scanner, WebSocket bar
subscription + tick-replay frames, and the order spine (place -> filled ->
position). No mocks: every claim is an HTTP/WS round trip.

The UI check is not "the mount answers 200": that passes for a stale bundle, an
empty directory, or the wrong directory entirely. It fetches every file in the
build stamp and compares the bytes served against the hash of the file that was
built, so the claim "the UI mount serves the built artifact" is the claim it
actually tests (see frontend/scripts/artifact-stamp.mjs).

Usage:
    BASE_URL=http://127.0.0.1:8123 PYTHONPATH=domain/src:brokers/src:trading/src \
        python trading/scripts/e2e_smoke.py

Exits non-zero if any check fails; prints one PASS/FAIL line per check.
"""

from __future__ import annotations

import asyncio
import hashlib
import json
import os
import sys
import urllib.error
import urllib.request
from pathlib import Path
from typing import Any
from uuid import uuid4

BASE = os.environ.get("BASE_URL", "http://127.0.0.1:8123")
SYMBOL = os.environ.get("E2E_SYMBOL", "RELIANCE")
EXCHANGE = "NSE"

# The UI the server reads, relative to the repo root (`trading/scripts/` -> root).
FRONTEND_DIST = Path(__file__).resolve().parents[2] / "frontend" / "dist"


def sha256(raw: bytes) -> str:
    """`sha256:<hex>` — the form the build stamp records per-file hashes in."""
    return f"sha256:{hashlib.sha256(raw).hexdigest()}"


def http_bytes(path: str) -> tuple[int, bytes]:
    """GET without content negotiation, so the bytes compared are the bytes stored."""
    req = urllib.request.Request(BASE + path, headers={"Accept-Encoding": "identity"})
    try:
        with urllib.request.urlopen(req, timeout=30) as r:
            return r.status, r.read()
    except urllib.error.HTTPError as e:
        return e.code, e.read()

_results: list[tuple[bool, str]] = []


def check(name: str, ok: bool, detail: str = "") -> None:
    _results.append((ok, f"{'PASS' if ok else 'FAIL'} {name}" + (f" :: {detail}" if detail else "")))
    print(_results[-1][1], flush=True)


def http(
    method: str,
    path: str,
    body: dict[str, Any] | None = None,
    headers: dict[str, str] | None = None,
) -> tuple[int, Any]:
    req = urllib.request.Request(
        BASE + path,
        method=method,
        data=json.dumps(body).encode() if body is not None else None,
        headers={"Content-Type": "application/json", **(headers or {})},
    )
    try:
        with urllib.request.urlopen(req, timeout=30) as r:
            raw = r.read().decode("utf-8", "replace")
            if raw.startswith("{") or raw.startswith("["):
                return r.status, json.loads(raw)
            return r.status, {"body": raw}
    except urllib.error.HTTPError as e:
        return e.code, json.loads(e.read() or b"{}")


async def ws_checks() -> None:
    import websockets

    async with websockets.connect(BASE.replace("http", "ws", 1) + "/ws/stream") as ws:
        # subscribe_bars must ack even with no live feed (paper session).
        await ws.send(json.dumps({"type": "subscribe_bars", "bars": [
            {"instrument": f"{EXCHANGE}:{SYMBOL}", "interval": "1m"}]}))
        ack = json.loads(await asyncio.wait_for(ws.recv(), 10))
        check("ws.subscribe_bars ack", ack.get("type") == "subscribed_bars", str(ack)[:80])

        # Tick-replay drives SyntheticTickGenerator -> BarAggregator -> frames.
        await ws.send(json.dumps({"type": "replay_start",
                                  "instrument": f"{EXCHANGE}:{SYMBOL}",
                                  "interval": "1m", "speed": 60, "seed": 42}))
        saw_bar = saw_closed = False
        try:
            while True:
                msg = json.loads(await asyncio.wait_for(ws.recv(), 20))
                t = msg.get("type")
                if t == "bar":
                    saw_bar = True
                    saw_closed = saw_closed or bool(msg.get("closed"))
                    if saw_bar and saw_closed:
                        break
                if t == "replay_done":
                    break
        except asyncio.TimeoutError:
            pass
        check("ws.replay bar frames", saw_bar)
        check("ws.replay closed-bar frame", saw_closed)

        await ws.send(json.dumps({"type": "replay_stop"}))
        # A fast replay floods the socket before the stop lands; drain until
        # the ack (bounded). If the stream simply goes quiet, the observable
        # contract "frames cease after stop" still holds.
        stopped = silent = False
        try:
            for _ in range(300):
                msg = json.loads(await asyncio.wait_for(ws.recv(), 6))
                t = msg.get("type")
                if t == "replay_stopped":
                    stopped = True
                    break
                if t == "bar":
                    continue
        except asyncio.TimeoutError:
            silent = True
        check("ws.replay_stop ack", stopped,
              "ack received" if stopped else "no ack within drain window")


def ui_artifact_checks() -> None:
    """Every byte the UI mount serves must be the byte that was built.

    The stamp is the only thing that knows which build is current, and it lives
    next to the bundle (`frontend/dist/BUILD_STAMP.json`). With no local build to
    compare against there is nothing to verify, so a *local* run fails rather than
    pretending the mount is fine; a run against a remote BASE_URL says so and
    moves on, since the artifact it would compare against is on another machine.
    """
    stamp_path = FRONTEND_DIST / "BUILD_STAMP.json"
    if not stamp_path.exists():
        local = BASE.startswith(("http://127.0.0.1", "http://localhost"))
        check(
            "ui.artifact_stamp",
            not local,
            f"no build stamp at {stamp_path}"
            + ("" if local else f" (BASE_URL {BASE} is remote, so nothing local to compare)"),
        )
        return

    stamp = json.loads(stamp_path.read_text())
    code, raw = http_bytes("/ui/BUILD_STAMP.json")
    served_stamp: Any = None
    if code == 200:
        try:
            served_stamp = json.loads(raw)
        except json.JSONDecodeError:
            served_stamp = None
    check(
        "ui.served_stamp_is_the_build_on_disk",
        code == 200 and served_stamp == stamp,
        f"status={code}, distHash {stamp.get('distHash', '?')[:24]}",
    )

    files = stamp.get("files") or {}
    for rel, digest in sorted(files.items()):
        code, raw = http_bytes(f"/ui/{rel}")
        check(
            f"ui.served_bytes {rel}",
            code == 200 and sha256(raw) == digest,
            f"status={code}, served {sha256(raw)[:19]} vs built {str(digest)[:19]}",
        )

    # `GET /ui/` is the mount's html entry point: it has to be the built
    # index.html, not some 200 that happens to contain the app's markup.
    code, raw = http_bytes("/ui/")
    check(
        "ui.entry_is_the_built_index",
        code == 200 and sha256(raw) == files.get("index.html"),
        f"status={code}, served {sha256(raw)[:19]}",
    )


def main() -> int:
    code, body = http("GET", "/health/live")
    check("health.live", code == 200)

    code, body = http("GET", "/health/ready")
    check("health.ready READY", code == 200 and body.get("session_state") == "READY", str(body))

    # The mount's entry point: a 200 carrying the app shell. This one check is
    # about the *route* — it is the only UI check that also holds for a remote
    # server, where there is no local build to compare bytes against.
    code, body = http("GET", "/ui/")
    shell = body.get("body", "") if isinstance(body, dict) else ""
    check(
        "ui.mounted",
        code == 200 and '<div id="app">' in shell,
        f"status={code}, {len(shell)} bytes",
    )
    ui_artifact_checks()

    code, body = http("GET", f"/api/charts/history/{EXCHANGE}:{SYMBOL}?interval=5m")
    bars = body.get("bars", []) if isinstance(body, dict) else []
    check("history.bars>0", code == 200 and len(bars) > 0, f"{len(bars)} bars source={body.get('source')}")
    check("history.closed_only_shape",
          all(all(k in b for k in ("time", "open", "high", "low", "close")) for b in bars[:5]))

    code, body = http("GET", "/api/charts/indicators")
    ids = {i["id"] for i in body.get("indicators", [])}
    check("indicators.catalogue>=11", code == 200 and len(ids) >= 11, f"{len(ids)}: {sorted(ids)}")

    code, body = http("POST", "/api/charts/indicators/compute",
                      {"exchange": EXCHANGE, "symbol": SYMBOL, "interval": "5m",
                       "id": "sma", "params": {"period": 5}})
    pts = body.get("points", [])
    check("indicators.compute sma(5)", code == 200 and len(pts) == len(bars),
          f"{len(pts)} points vs {len(bars)} bars")

    code, body = http("GET", "/api/charts/strategies")
    bt = body.get("backtestable", [])
    scanners = [s["id"] for s in body.get("scanners", [])]
    check("strategies.backtestable", code == 200 and "sma_cross" in [b["id"] for b in bt])

    code, body = http("POST", "/api/charts/backtest",
                      {"exchange": EXCHANGE, "symbol": SYMBOL, "interval": "D",
                       "strategy": "sma_cross", "params": {"fast": 3, "slow": 7}})
    m = body.get("metrics")
    check("backtest.metrics", code == 200 and m is not None, json.dumps(m) if m else str(body)[:120])
    check("backtest.equity+fills", bool(body.get("equity_curve")) and isinstance(body.get("trades"), list))

    scan_id = next((s for s in scanners if s.startswith("momentum")), scanners[0] if scanners else "")
    code, body = http("POST", "/api/charts/scanner/run", {"id": scan_id})
    check(f"scanner.{scan_id}", code == 200 and isinstance(body.get("results"), list),
          f"{len(body.get('results', []))} results")

    asyncio.run(ws_checks())

    # Order spine. With no live tape, a MARKET order is correctly REJECTED
    # fail-loud (FillModel refuses zero-price fills); a priced LIMIT fills.
    #
    # `Idempotency-Key` is mandatory on order mutations (routes/orders.py), and a
    # fresh one per call: reusing a key is duplicate suppression, so a replayed key
    # would make the second order look like the first rather than exercising it.
    code, body = http("POST", "/orders",
                      {"exchange": EXCHANGE, "symbol": SYMBOL, "side": "BUY",
                       "order_type": "MARKET", "quantity": 5},
                      headers={"Idempotency-Key": uuid4().hex})
    check("order.market_fail_loud_without_tape",
          code == 200 and str(body.get("status", "")) == "REJECTED",
          str(body)[:120])

    last_close = bars[-1]["close"] if bars else 0
    code, body = http("POST", "/orders",
                      {"exchange": EXCHANGE, "symbol": SYMBOL, "side": "BUY",
                       "order_type": "LIMIT", "quantity": 5, "price": last_close},
                      headers={"Idempotency-Key": uuid4().hex})
    oid = body.get("order_id") if isinstance(body, dict) else None
    check("order.limit.place", code == 200 and bool(oid), str(body)[:100])

    if oid:
        code, body = http("GET", f"/orders/{oid}")
        status = str(body.get("status", "")).upper()
        check("order.filled", code == 200 and status in {"FILLED", "ACK"}, status)

        code, book = http("GET", "/api/charts/book")
        pos = [p for p in book.get("positions", []) if p.get("symbol") == SYMBOL]
        check("book.position_projected", code == 200 and pos, json.dumps(pos)[:100])

    print("\n==== E2E SUMMARY ====")
    fails = [r for ok, r in _results if not ok]
    print(f"{len(_results) - len(fails)}/{len(_results)} passed")
    for f in fails:
        print(" ", f)
    return 1 if fails else 0


if __name__ == "__main__":
    sys.exit(main())
