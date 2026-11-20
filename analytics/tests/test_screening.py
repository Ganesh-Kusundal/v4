"""Tests for screening pipeline."""

from tradex_analytics.screening import (
    UniverseFilter,
    FundamentalFilter,
    TechnicalFilter,
    CustomScreener,
    RankingEngine,
    ScreeningPipeline,
    ScreenResult,
)


def _candidates():
    return [
        {"symbol": "AAA", "sector": "IT", "market_cap": 1000, "pe": 15, "roe": 20, "rsi": 55, "price": 100, "sma_20": 95, "volume_ratio": 1.5},
        {"symbol": "BBB", "sector": "Bank", "market_cap": 500, "pe": 8, "roe": 12, "rsi": 45, "price": 50, "sma_20": 55, "volume_ratio": 0.8},
        {"symbol": "CCC", "sector": "IT", "market_cap": 2000, "pe": 30, "roe": 8, "rsi": 75, "price": 200, "sma_20": 190, "volume_ratio": 2.0},
    ]


def test_universe_filter_sector():
    f = UniverseFilter(allowed_sectors=["IT"])
    cands = _candidates()
    passed = [c for c in cands if f.apply(c)]
    assert len(passed) == 2
    assert all(c["sector"] == "IT" for c in passed)


def test_universe_filter_market_cap():
    f = UniverseFilter(min_market_cap=800)
    cands = _candidates()
    passed = [c for c in cands if f.apply(c)]
    assert len(passed) == 2


def test_fundamental_filter_pe():
    f = FundamentalFilter(max_pe=20)
    cands = _candidates()
    passed = [c for c in cands if f.apply(c)]
    assert len(passed) == 2  # AAA (15), BBB (8)


def test_technical_filter_rsi():
    f = TechnicalFilter(max_rsi=60)
    cands = _candidates()
    passed = [c for c in cands if f.apply(c)]
    assert len(passed) == 2  # AAA (55), BBB (45)


def test_technical_filter_price_above_sma():
    f = TechnicalFilter(price_above_sma=True)
    cands = _candidates()
    passed = [c for c in cands if f.apply(c)]
    # AAA: 100>95 yes; BBB: 50>55 no; CCC: 200>190 yes
    assert len(passed) == 2


def test_custom_screener():
    f = CustomScreener(expression=lambda c: c["pe"] < 20 and c["rsi"] > 50)
    cands = _candidates()
    passed = [c for c in cands if f.apply(c)]
    assert len(passed) == 1  # AAA


def test_ranking_engine():
    ranker = RankingEngine(factor_weights={"roe": 1.0, "pe": -0.5})
    results = ranker.rank(_candidates())
    assert len(results) == 3
    assert results[0].score >= results[1].score >= results[2].score or True
    # Just check sorted descending
    scores = [r.score for r in results]
    assert scores == sorted(scores, reverse=True)


def test_screening_pipeline():
    filters = [UniverseFilter(allowed_sectors=["IT"]), FundamentalFilter(max_pe=20)]
    pipeline = ScreeningPipeline(filters=filters)
    results = pipeline.run(_candidates())
    # Only AAA passes (IT + pe<=20)
    assert len(results) == 1
    assert results[0].symbol == "AAA"


def test_screen_result_dataclass():
    r = ScreenResult(symbol="X", score=1.5, passed=True)
    assert r.symbol == "X"
    assert r.score == 1.5
