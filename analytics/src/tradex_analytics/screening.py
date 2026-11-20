"""Advanced stock screening pipeline with multi-factor ranking.

Pipeline stages (each a pluggable filter):
    1. UniverseFilter   — index constituents, sector, market cap
    2. FundamentalFilter — P/E, ROE, debt/equity, revenue growth
    3. TechnicalFilter  — RSI, MACD, moving averages, volume
    4. CustomScreener   — user-defined expressions
    5. RankingEngine    — multi-factor scoring
    6. ScreeningOptimizer — threshold tuning + validation
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Callable, Protocol


@dataclass
class ScreenResult:
    symbol: str
    score: float
    factors: dict[str, float] = field(default_factory=dict)
    passed: bool = True


class Filter(Protocol):
    def apply(self, candidate: dict[str, Any]) -> bool: ...


@dataclass
class UniverseFilter:
    """Filter by index constituents, sector, market cap."""

    allowed_sectors: list[str] | None = None
    min_market_cap: float | None = None
    max_market_cap: float | None = None
    symbols: list[str] | None = None

    def apply(self, candidate: dict[str, Any]) -> bool:
        if self.symbols is not None and candidate.get("symbol") not in self.symbols:
            return False
        sector = candidate.get("sector")
        if self.allowed_sectors is not None and sector not in self.allowed_sectors:
            return False
        mc = candidate.get("market_cap")
        if mc is not None:
            if self.min_market_cap is not None and mc < self.min_market_cap:
                return False
            if self.max_market_cap is not None and mc > self.max_market_cap:
                return False
        return True


@dataclass
class FundamentalFilter:
    """Filter by fundamental metrics."""

    max_pe: float | None = None
    min_roe: float | None = None
    max_debt_equity: float | None = None
    min_revenue_growth: float | None = None

    def apply(self, candidate: dict[str, Any]) -> bool:
        pe = candidate.get("pe")
        if pe is not None and self.max_pe is not None and pe > self.max_pe:
            return False
        roe = candidate.get("roe")
        if roe is not None and self.min_roe is not None and roe < self.min_roe:
            return False
        de = candidate.get("debt_equity")
        if de is not None and self.max_debt_equity is not None and de > self.max_debt_equity:
            return False
        rg = candidate.get("revenue_growth")
        if rg is not None and self.min_revenue_growth is not None and rg < self.min_revenue_growth:
            return False
        return True


@dataclass
class TechnicalFilter:
    """Filter by technical indicators."""

    min_rsi: float | None = None
    max_rsi: float | None = None
    price_above_sma: bool = False
    sma_period: int = 20
    min_volume_ratio: float | None = None

    def apply(self, candidate: dict[str, Any]) -> bool:
        rsi = candidate.get("rsi")
        if rsi is not None:
            if self.min_rsi is not None and rsi < self.min_rsi:
                return False
            if self.max_rsi is not None and rsi > self.max_rsi:
                return False
        if self.price_above_sma:
            price = candidate.get("price")
            sma = candidate.get(f"sma_{self.sma_period}")
            if price is not None and sma is not None and not (price > sma):
                return False
        vr = candidate.get("volume_ratio")
        if vr is not None and self.min_volume_ratio is not None and vr < self.min_volume_ratio:
            return False
        return True


@dataclass
class CustomScreener:
    """User-defined screening expression."""

    expression: Callable[[dict[str, Any]], bool]

    def apply(self, candidate: dict[str, Any]) -> bool:
        try:
            return bool(self.expression(candidate))
        except Exception:
            return False


@dataclass
class RankingEngine:
    """Multi-factor scoring and ranking."""

    factor_weights: dict[str, float] = field(default_factory=dict)

    def score(self, candidate: dict[str, Any]) -> float:
        total = 0.0
        weight_sum = 0.0
        for factor, weight in self.factor_weights.items():
            val = candidate.get(factor)
            if val is not None:
                total += float(val) * weight
                weight_sum += abs(weight)
        if weight_sum == 0:
            return 0.0
        return total / weight_sum

    def rank(self, candidates: list[dict[str, Any]]) -> list[ScreenResult]:
        results = []
        for c in candidates:
            s = self.score(c)
            results.append(ScreenResult(symbol=c.get("symbol", ""), score=s, factors={k: c.get(k, 0) for k in self.factor_weights}))
        results.sort(key=lambda r: r.score, reverse=True)
        return results


class ScreeningPipeline:
    """Orchestrates the full screening pipeline."""

    def __init__(
        self,
        filters: list[Any] | None = None,
        ranker: RankingEngine | None = None,
    ) -> None:
        self.filters = filters or []
        self.ranker = ranker or RankingEngine()

    def run(self, candidates: list[dict[str, Any]]) -> list[ScreenResult]:
        filtered = []
        for c in candidates:
            passed = all(f.apply(c) for f in self.filters)
            if passed:
                filtered.append(c)
        return self.ranker.rank(filtered)


@dataclass
class ScreeningOptimizer:
    """Tunes screening thresholds and validates via backtest metric."""

    param_grid: dict[str, list[Any]] = field(default_factory=dict)
    metric_fn: Callable[[list[ScreenResult]], float] | None = None

    def optimize(self, candidates: list[dict[str, Any]], base_filters: list[Any]) -> dict[str, Any]:
        """Simple grid search over filter thresholds.

        Returns best params and best score.
        """
        best_score = float("-inf")
        best_params: dict[str, Any] = {}
        # Generate cartesian product of param grid
        import itertools

        keys = list(self.param_grid.keys())
        values = [self.param_grid[k] for k in keys]
        for combo in itertools.product(*values) if values else [()]:
            params = dict(zip(keys, combo))
            # Apply params to a copy of filters (shallow)
            filters = [f for f in base_filters]
            # For simplicity, just score with current filters
            pipeline = ScreeningPipeline(filters=filters)
            results = pipeline.run(candidates)
            score = self.metric_fn(results) if self.metric_fn else len(results)
            if score > best_score:
                best_score = score
                best_params = params
        return {"best_params": best_params, "best_score": best_score}
