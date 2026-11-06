"""Parity: the 15:30 end-of-day flush reaches the strategy (A5)."""

from __future__ import annotations

from datetime import datetime, timezone
from decimal import Decimal

import pytest
from tradex_config.schema import AppConfig
from tradex_domain import Candle, Equity, Price, Quantity, Quote, Timeframe
from tradex_runtime.startup import boot
from trading.tests.risk_support import permissive_risk

INSTRUMENT = Equity.of("NSE", "RELIANCE")


@pytest.mark.xfail(strict=True, reason="A5: 15:30 flush reaches strategy")
def test_eod_flush_reaches_strategy() -> None:
    config = AppConfig(
        risk=permissive_risk(), mode="paper", feed_instruments=(INSTRUMENT,)
    )
    session = boot(config)

    seen: list[Candle] = []
    session.bus.of_type(Candle).subscribe(seen.append)

    try:
        quote = Quote(
            instrument=INSTRUMENT,
            ltp=Price(value=Decimal("100")),
            volume=Quantity(value=Decimal("1")),
            timestamp=datetime(2026, 9, 24, 10, 15, tzinfo=timezone.utc),
            exchange="NSE",
            provider="sim",
        )
        session.bus.publish(quote)
        # Simulate the 15:30 flat-close flush that production should own.
        try:
            session.stop()
        except Exception:  # noqa: BLE001 - teardown may raise today
            pass
        assert len(seen) >= 1, "the final bar never reached the strategy"
        # The flushed (closed) bar must be among them.
        assert any(c.ohlc.close.value == Decimal("100") for c in seen)
    finally:
        try:
            session.stop()
        except Exception:  # noqa: BLE001
            pass
