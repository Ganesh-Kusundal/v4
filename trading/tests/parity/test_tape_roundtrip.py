"""Parity: a recorded tape round-trips to an identical fill stream."""

from __future__ import annotations

from datetime import datetime
from decimal import Decimal
from pathlib import Path
from zoneinfo import ZoneInfo

import pytest
from tradex_domain import OrderSide, Timeframe
from tradex_domain.enums import OrderType
from tradex_domain.execution import OrderRequest
from tradex_domain.instruments import Equity
from tradex_domain.market import Quote
from tradex_domain.value_objects import Price, Quantity
from tradex_execution.fill_sources import PaperFillSource
from tradex_execution.trading_cache import TradingCache
from tradex_runtime.tape_recorder import (
    TAPE_RECORD_ENV_VAR,
    TapeRecorder,
    read_tape,
)

IST = ZoneInfo("Asia/Kolkata")
INSTRUMENT = Equity.of("NSE", "RELIANCE")


def _stamp(hms: str) -> datetime:
    hour, minute, second = (int(part) for part in hms.split(":"))
    return datetime(2026, 9, 24, hour, minute, second, tzinfo=IST)


def _quote(ts: datetime, ltp: str) -> Quote:
    return Quote(
        instrument=INSTRUMENT,
        ltp=Price(value=Decimal(ltp)),
        volume=Quantity(value=Decimal("1000")),
        timestamp=ts,
        exchange="NSE",
        provider="dhan",
    )


def _request() -> OrderRequest:
    return OrderRequest(
        instrument=INSTRUMENT,
        side=OrderSide.BUY,
        order_type=OrderType.MARKET,
        quantity=Quantity(value=Decimal("10")),
    )


def test_tape_roundtrip_identical_fill_stream(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv(TAPE_RECORD_ENV_VAR, "1")
    rows = [("10:15:00", "2500.50"), ("10:15:10", "2501.25"), ("10:15:20", "2502.00")]
    path = tmp_path / "tape.jsonl"
    with TapeRecorder(path, broker="dhan") as recorder:
        for hms, ltp in rows:
            recorder.record(_quote(_stamp(hms), ltp))

    ticks = read_tape(path)
    assert len(ticks) == len(rows)

    # Replay twice through the paper fill path; fill streams must be identical.
    def replay() -> list[Decimal]:
        cache = TradingCache()
        fills: list[Decimal] = []
        for tick in ticks:
            cache.update_quote(tick.to_quote())
            _, fill = PaperFillSource(cache=cache).submit(_request())
            assert fill is not None
            fills.append(fill.price.value)
        return fills

    first = replay()
    second = replay()
    assert first == second
    assert first == [Decimal(p) for _, p in rows]
