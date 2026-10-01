"""MarketWindowLoader is the one door for history/backtest/replay windows.

The property under test: the same ``(instrument, timeframe, start, end)``
resolved through the loader is identical regardless of which caller asked —
chart history, backtest, or replay. Provenance travels with the result so a
broker fallback cannot silently look like a datalake hit.
"""

from __future__ import annotations

from datetime import datetime, timedelta
from decimal import Decimal

from tradex_domain.enums import Timeframe
from tradex_domain.instruments import Equity
from tradex_domain.market import OHLC, Candle
from tradex_domain.value_objects import Price, Quantity
from tradex_market_data.window_loader import (
    MarketSource,
    MarketWindowLoader,
)


def _candle(ts: datetime, price: str = "100") -> Candle:
    p = Price(value=Decimal(price))
    return Candle(
        instrument=Equity.of("NSE", "TEST"),
        timeframe=Timeframe.M1,
        ohlc=OHLC(open=p, high=p, low=p, close=p),
        volume=Quantity(value=Decimal("10")),
        timestamp=ts,
    )


class _FakeProvider:
    def __init__(self, candles: list[Candle]) -> None:
        self._candles = candles
        self.calls: list[tuple] = []

        class _Store:
            def date_range(self, symbol):
                if not candles:
                    return None
                return (candles[0].timestamp, candles[-1].timestamp)

        self.store = _Store()

    def history(self, instrument, timeframe, start, end):
        self.calls.append((instrument, timeframe, start, end))
        in_window = [
            c for c in self._candles if start <= c.timestamp <= end
        ]
        class S:
            pass
        s = S()
        s.candles = in_window
        return s


class TestMarketWindowLoader:
    def test_datalake_hit_reports_provenance(self) -> None:
        base = datetime(2026, 1, 5, 9, 15)
        candles = [_candle(base + timedelta(minutes=i)) for i in range(5)]
        provider = _FakeProvider(candles)
        loader = MarketWindowLoader(provider=provider)  # type: ignore[arg-type]
        window = loader.load(
            Equity.of("NSE", "TEST"),
            Timeframe.M1,
            base,
            base + timedelta(minutes=10),
        )
        assert window.source is MarketSource.DATALAKE
        assert len(window.candles) == 5
        assert not window.empty

    def test_empty_lake_without_broker_is_none(self) -> None:
        provider = _FakeProvider([])
        loader = MarketWindowLoader(provider=provider)  # type: ignore[arg-type]
        window = loader.load(
            Equity.of("NSE", "TEST"),
            Timeframe.M1,
            datetime(2026, 1, 5, 9, 15),
            datetime(2026, 1, 5, 15, 30),
        )
        assert window.source is MarketSource.NONE
        assert window.empty

    def test_broker_fallback_only_when_allowed(self) -> None:
        base = datetime(2026, 1, 5, 9, 15)
        broker_candles = [_candle(base, "200")]
        provider = _FakeProvider([])
        loader = MarketWindowLoader(
            provider=provider,  # type: ignore[arg-type]
            broker_history=lambda *a: broker_candles,
        )
        denied = loader.load(
            Equity.of("NSE", "TEST"), Timeframe.M1, base, base + timedelta(minutes=1)
        )
        assert denied.source is MarketSource.NONE
        allowed = loader.load(
            Equity.of("NSE", "TEST"),
            Timeframe.M1,
            base,
            base + timedelta(minutes=1),
            allow_broker=True,
        )
        assert allowed.source is MarketSource.BROKER
        assert allowed.candles[0].ohlc.close.value == Decimal("200")

    def test_replay_and_direct_load_agree_on_the_same_window(self) -> None:
        """The data gate: replay and a direct load see the same candles."""
        base = datetime(2026, 1, 5, 9, 15)
        candles = [_candle(base + timedelta(minutes=i)) for i in range(10)]
        provider = _FakeProvider(candles)
        loader = MarketWindowLoader(provider=provider)  # type: ignore[arg-type]

        direct = loader.load(
            Equity.of("NSE", "TEST"),
            Timeframe.M1,
            base,
            base + timedelta(minutes=9),
        )
        # Pin "now" so the replay window is the same absolute range.
        replay = loader.load_replay_m1(
            Equity.of("NSE", "TEST"),
            minutes=9,
            now=base + timedelta(minutes=9),
        )
        assert [c.timestamp for c in direct.candles] == [
            c.timestamp for c in replay.candles
        ]
        assert direct.source is replay.source is MarketSource.DATALAKE
