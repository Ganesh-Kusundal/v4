"""Gap tests for FeaturePipeline — builtin features and history."""

from __future__ import annotations

from datetime import UTC, datetime
from decimal import Decimal

from tradex_domain import OHLC, Candle, Price, Quantity, Timeframe
from tradex_domain.events import CandleReceived
from tradex_domain.instruments import Equity

from tradex_trading.analytics.feature_pipeline import FeaturePipeline
from tradex_trading.reactive.bus import ReactiveBus


def _make_candle(symbol: str, o: str, h: str, lo: str, c: str) -> CandleReceived:
    instrument = Equity.of("NSE", symbol)
    candle = Candle(
        instrument=instrument,
        timeframe=Timeframe.D1,
        ohlc=OHLC(
            open=Price(Decimal(o)),
            high=Price(Decimal(h)),
            low=Price(Decimal(lo)),
            close=Price(Decimal(c)),
        ),
        volume=Quantity(Decimal("1000")),
        timestamp=datetime.now(UTC),
    )
    return CandleReceived(candle=candle)


def test_feature_pipeline_computes_builtin_features() -> None:
    """Default FeaturePipeline computes typical_price, price_range, body_size."""
    bus = ReactiveBus()
    pipe = FeaturePipeline(bus)
    bus.publish(_make_candle("BUILTIN", "100", "120", "80", "110"))
    feats = pipe.get_features("BUILTIN")
    # typical_price = (120 + 80 + 110) / 3 = 103.333...
    assert feats["typical_price"] == (Decimal("120") + Decimal("80") + Decimal("110")) / 3
    # price_range = 120 - 80 = 40
    assert feats["price_range"] == Decimal("40")
    # body_size = abs(110 - 100) = 10
    assert feats["body_size"] == Decimal("10")


def test_feature_pipeline_feature_history() -> None:
    """Feature history accumulates across multiple candles."""
    bus = ReactiveBus()
    pipe = FeaturePipeline(bus)
    bus.publish(_make_candle("HIST2", "100", "110", "90", "105"))
    bus.publish(_make_candle("HIST2", "105", "115", "95", "110"))
    bus.publish(_make_candle("HIST2", "110", "125", "100", "120"))
    history = pipe.feature_history("HIST2")
    assert len(history) == 3
    # Each entry should have all default features
    for entry in history:
        assert "typical_price" in entry
        assert "price_range" in entry
        assert "body_size" in entry
    # body_size should increase: 5, 5, 10
    assert history[0]["body_size"] == Decimal("5")
    assert history[2]["body_size"] == Decimal("10")
