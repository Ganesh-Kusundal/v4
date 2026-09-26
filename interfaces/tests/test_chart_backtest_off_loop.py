"""``POST /api/charts/backtest`` must not hold the event loop.

Defect (High): ``routes/chart.py::run_backtest`` is ``async def`` and called
``BacktestEngine.run(...)`` straight onto the loop. A backtest over a long
window therefore blocks the loop for its whole duration — and that loop is the
one ``/ws/stream`` runs on (``routes/stream.py::ws_stream`` is the same uvicorn
loop), so the live quote/order/position feed stalls for the length of the
backtest. The scanner route in the same file already knew this
(``await asyncio.to_thread(engine.run, definition)``).

The invariant pinned here is behavioural, not structural: while the engine is
running, a heartbeat task on the *same* loop must still get to run. That is
what a stalled WebSocket feed is made of, so it is what is worth pinning — a
grep for ``to_thread`` would only pin the current spelling of the fix.

Determinism, and where the care went:

* The engine stub parks on a ``threading.Event`` and times out on **its own
  thread's** clock. That is load-bearing. An ``asyncio.wait_for`` around the
  request lives on the very loop the defect wedges, so it could never fire —
  the test would hang instead of failing. A worker-thread wall clock is the
  only timeout that still expires while the loop is blocked.
* The heartbeat advances on bare ``asyncio.sleep(0)`` — no wall-clock at all.
  It first spins (without blocking) until the engine reports it has been
  entered, so it cannot tick before the request is even in flight; the naive
  version passes on a broken build for exactly that reason, because the loop
  hands out a few slices during the request's own setup.
* ``before = ticks`` is captured *before* the entered-signal, so no tick can
  land on the wrong side of the measurement.

The second test pins the part a thread hop could plausibly break: an
``HTTPException`` raised inside the engine must still arrive as its own status
code with the app's typed-error body, not as a 500.
"""

from __future__ import annotations

import asyncio
import threading
from datetime import datetime
from typing import Any

import pytest

fastapi = pytest.importorskip("fastapi")
httpx = pytest.importorskip("httpx")

import tradex_interfaces.routes.chart as chart_module  # noqa: E402
import tradex_replay.backtest as backtest_module  # noqa: E402
from fastapi import HTTPException  # noqa: E402
from tradex_interfaces.fastapi_app import create_app  # noqa: E402

#: How long the engine stub waits for the heartbeat before concluding the loop
#: is wedged. Only ever spent on a FAILING run — in a passing build the
#: heartbeat releases it in a fraction of a millisecond — so it is set for
#: tolerance on a loaded machine, not for speed.
_ENGINE_BUDGET_SECONDS = 5.0

_BODY = {
    "exchange": "NSE",
    "symbol": "RELIANCE",
    "interval": "D",
    "strategy": "sma_cross",
}


class _StubResult:
    """The slice of ``BacktestResult`` the route reads back."""

    total_return = 0.0
    sharpe = 0.0
    max_drawdown = 0.0
    num_trades = 0
    num_rejected = 0
    total_fees = 0.0
    equity_curve: list[float] = []
    fills: list[dict[str, Any]] = []


def _stub_candles() -> list[Any]:
    from tradex_brokers.common.market_builders import make_candle
    from tradex_domain.enums import Timeframe
    from tradex_domain.instruments import Equity

    instrument = Equity.of("NSE", "RELIANCE")
    return [
        make_candle(
            instrument,
            Timeframe.D1,
            open=100.0,
            high=101.0,
            low=99.0,
            close=100.5,
            volume=1_000,
            timestamp=datetime(2026, 6, day),
        )
        for day in (1, 2, 3)
    ]


@pytest.fixture(autouse=True)
def _stub_datalake_read(monkeypatch):
    """The engine stub, not the lake, is what these tests exercise.

    The window the route asks for must return bars or it early-returns with
    ``{"metrics": None}`` and never reaches the engine — which would make the
    heartbeat test pass without having exercised the defect at all.
    """
    monkeypatch.setattr(
        chart_module, "_backtest_candles", lambda *args, **kwargs: _stub_candles()
    )


async def _post_backtest() -> httpx.Response:
    transport = httpx.ASGITransport(app=create_app(session=None))
    async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
        return await client.post("/api/charts/backtest", json=_BODY)


def test_backtest_route_leaves_the_event_loop_free(monkeypatch):
    """The loop keeps running while the engine does.

    Pins the shared-loop property the WebSocket feed depends on: the heartbeat
    reaches a tick *while the engine is still inside run()*, which can only
    happen if the route awaits a thread rather than running the engine on the
    loop.
    """
    ticks = 0
    observed: list[int] = []
    entered = threading.Event()
    release = threading.Event()

    async def heartbeat() -> None:
        nonlocal ticks
        while not entered.is_set():
            # Spin without blocking, so a free loop can still get here.
            await asyncio.sleep(0)
        await asyncio.sleep(0)
        ticks += 1
        release.set()

    def blocking_run(self: Any, strategy: Any, candles: Any) -> _StubResult:
        nonlocal ticks
        before = ticks  # captured before the signal, so no tick lands early
        entered.set()
        release.wait(_ENGINE_BUDGET_SECONDS)
        # Read on the worker's own thread: what the loop managed to do while
        # this was parked. Zero means the loop never got a slice.
        observed.append(ticks - before)
        return _StubResult()

    class _BlockingEngine:
        def __init__(self, *args: Any, **kwargs: Any) -> None:
            pass

        run = blocking_run

    monkeypatch.setattr(backtest_module, "BacktestEngine", _BlockingEngine)

    async def scenario() -> httpx.Response:
        beat = asyncio.create_task(heartbeat())
        try:
            return await _post_backtest()
        finally:
            await beat

    response = asyncio.run(scenario())

    assert observed, (
        "the route never called BacktestEngine.run — the heartbeat test would "
        "have passed without exercising the defect"
    )
    assert observed[0] > 0, (
        "the event loop was blocked for the whole backtest: a concurrent "
        "/ws/stream feed runs on that same loop and would have stalled with it"
    )
    assert response.status_code == 200, response.text
    assert response.json()["metrics"] is not None


def test_http_exception_from_the_engine_thread_keeps_its_status(monkeypatch):
    """The thread hop must not launder an engine error into a 500.

    ``asyncio.to_thread`` re-raises the original exception object, so the
    app's registered ``HTTPException`` handler still sees it. This is the
    cheap way to lose that: a backtest that reports a bad request as a server
    error is indistinguishable, to the caller, from a crash.
    """

    class _ExplodingEngine:
        def __init__(self, *args: Any, **kwargs: Any) -> None:
            pass

        def run(self, strategy: Any, candles: Any) -> _StubResult:
            raise HTTPException(status_code=409, detail="stale revision")

    monkeypatch.setattr(backtest_module, "BacktestEngine", _ExplodingEngine)

    response = asyncio.run(_post_backtest())

    assert response.status_code == 409, response.text
    assert response.json() == {
        "error": {"code": "conflict", "message": "stale revision"}
    }
