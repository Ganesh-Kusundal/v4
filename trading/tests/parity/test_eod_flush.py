"""Parity: a session's last open bar reaches the strategy (A5).

Production closes the book at 15:30 through ``SessionCloseFlush`` (its own
unit test covers the clock); teardown is the deterministic stand-in here,
because the alternative is a test that sleeps until the cash close.
"""

from __future__ import annotations

from datetime import UTC, datetime
from decimal import Decimal

from tradex_config.schema import AppConfig
from tradex_domain import Candle, Equity, Price, Quantity, Quote
from tradex_runtime.startup import boot
from trading.tests.risk_support import permissive_risk

INSTRUMENT = Equity.of("NSE", "RELIANCE")


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
            timestamp=datetime(2026, 9, 24, 10, 15, tzinfo=UTC),
            exchange="NSE",
            provider="sim",
        )
        session.bus.publish(quote)
        # The flush runs inside ``stop()`` (the aggregator's dispose closes the
        # open bucket), so the final bar is delivered before the bus dies.
        session.stop()
        assert len(seen) >= 1, "the final bar never reached the strategy"
        # The flushed (closed) bar must be among them.
        assert any(c.ohlc.close.value == Decimal("100") for c in seen)
    finally:
        try:
            session.stop()
        except Exception:  # noqa: BLE001
            pass
