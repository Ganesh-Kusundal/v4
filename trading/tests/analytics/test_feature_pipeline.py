"""Tests for FeaturePipeline."""

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


# ------------------------------------------------------------------ #
# 1. default features computed
# ------------------------------------------------------------------ #

def test_default_features_computed():
    bus = ReactiveBus()
    pipe = FeaturePipeline(bus)

    bus.publish(_make_candle("TEST", "100", "110", "95", "105"))

    feats = pipe.get_features("TEST")
    assert "typical_price" in feats
    assert "price_range" in feats
    assert "body_size" in feats
    # typical_price = (110 + 95 + 105) / 3 = 103.333...
    assert feats["typical_price"] == (Decimal("110") + Decimal("95") + Decimal("105")) / 3
    # price_range = 110 - 95 = 15
    assert feats["price_range"] == Decimal("15")
    # body_size = abs(105 - 100) = 5
    assert feats["body_size"] == Decimal("5")


# ------------------------------------------------------------------ #
# 2. custom features
# ------------------------------------------------------------------ #

def test_custom_features():
    bus = ReactiveBus()
    pipe = FeaturePipeline(bus, features=["typical_price", "body_size"])

    bus.publish(_make_candle("CUSTOM", "200", "220", "190", "210"))

    feats = pipe.get_features("CUSTOM")
    assert set(feats.keys()) == {"typical_price", "body_size"}
    assert "price_range" not in feats


# ------------------------------------------------------------------ #
# 3. feature history
# ------------------------------------------------------------------ #

def test_feature_history():
    bus = ReactiveBus()
    pipe = FeaturePipeline(bus)

    for c in ["100", "101", "102"]:
        bus.publish(_make_candle("HIST", "100", "110", "90", c))

    history = pipe.feature_history("HIST")
    assert len(history) == 3
    assert history[0]["body_size"] == Decimal("0")   # abs(100-100)
    assert history[1]["body_size"] == Decimal("1")   # abs(101-100)
    assert history[2]["body_size"] == Decimal("2")   # abs(102-100)


# ------------------------------------------------------------------ #
# 4. unknown symbol
# ------------------------------------------------------------------ #

def test_get_features_unknown_symbol():
    bus = ReactiveBus()
    pipe = FeaturePipeline(bus)
    assert pipe.get_features("NOPE") == {}


# ------------------------------------------------------------------ #
# 5. multiple instruments
# ------------------------------------------------------------------ #

def test_multiple_instruments():
    bus = ReactiveBus()
    pipe = FeaturePipeline(bus)

    bus.publish(_make_candle("AAA", "100", "110", "90", "105"))
    bus.publish(_make_candle("BBB", "200", "220", "180", "210"))

    a_feats = pipe.get_features("AAA")
    b_feats = pipe.get_features("BBB")

    assert a_feats["price_range"] == Decimal("20")   # 110 - 90
    assert b_feats["price_range"] == Decimal("40")   # 220 - 180
    assert pipe.feature_history("AAA") != pipe.feature_history("BBB")
