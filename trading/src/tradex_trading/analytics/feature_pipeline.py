"""Streaming feature computation pipeline.

Subscribes to CandleReceived events on a ReactiveBus and computes
configurable features from each candle, storing results keyed by
instrument symbol.
"""

from __future__ import annotations

from decimal import Decimal
from typing import Any

from rx import operators as ops
from tradex_domain.events import CandleReceived

# ------------------------------------------------------------------ #
# Built-in feature functions
# ------------------------------------------------------------------ #
# Each receives a Candle and returns a Decimal.

def _typical_price(candle: Any) -> Decimal:
    o = candle.ohlc
    return (o.high.value + o.low.value + o.close.value) / 3


def _price_range(candle: Any) -> Decimal:
    o = candle.ohlc
    return o.high.value - o.low.value


def _body_size(candle: Any) -> Decimal:
    o = candle.ohlc
    return abs(o.close.value - o.open.value)


_BUILTIN_FEATURES: dict[str, Any] = {
    "typical_price": _typical_price,
    "price_range": _price_range,
    "body_size": _body_size,
}

DEFAULT_FEATURES = ["typical_price", "price_range", "body_size"]


class FeaturePipeline:
    """Compute and cache features from streaming candles.

    Parameters
    ----------
    bus:
        A :class:`~tradex_trading.reactive.bus.ReactiveBus` instance.
    features:
        List of feature names to compute.  Defaults to
        ``["typical_price", "price_range", "body_size"]``.
    """

    def __init__(self, bus: Any, features: list[str] | None = None) -> None:
        self._bus = bus
        self._feature_names = features if features is not None else list(DEFAULT_FEATURES)
        self._feature_fns = {name: _BUILTIN_FEATURES[name] for name in self._feature_names}
        # Latest features per symbol
        self._latest: dict[str, dict[str, Decimal]] = {}
        # Full history per symbol
        self._history: dict[str, list[dict[str, Decimal]]] = {}

        # Subscribe to CandleReceived events
        bus.of_type(CandleReceived).pipe(
            ops.map(lambda ev: ev.candle),
        ).subscribe(on_next=self._compute)

    # ------------------------------------------------------------------ #
    # Internal
    # ------------------------------------------------------------------ #

    def _compute(self, candle: Any) -> None:
        symbol = candle.instrument.symbol
        feats: dict[str, Decimal] = {}
        for name, fn in self._feature_fns.items():
            feats[name] = fn(candle)
        self._latest[symbol] = feats
        self._history.setdefault(symbol, []).append(feats)

    # ------------------------------------------------------------------ #
    # Public API
    # ------------------------------------------------------------------ #

    def get_features(self, symbol: str) -> dict[str, Decimal]:
        """Return the latest computed features for *symbol*."""
        return dict(self._latest.get(symbol, {}))

    def feature_history(self, symbol: str) -> list[dict[str, Decimal]]:
        """Return all computed feature dicts for *symbol*."""
        return list(self._history.get(symbol, []))
