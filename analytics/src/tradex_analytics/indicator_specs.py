"""Built-in indicator parameter schemas, split out of ``indicators.py``."""

from __future__ import annotations

from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from tradex_analytics.indicators import IndicatorSpec


def builtin_specs() -> list[IndicatorSpec]:
    # Imported here, not at module level: indicators.py imports this module while it
    # is still initialising, so a top-level import back would be circular.
    from tradex_analytics._math import _source_values, _to_float
    from tradex_analytics.indicators import (
        IndicatorSpec,
        _fn_obv,
        _fn_vwap,
        _sma_seeded_ema,
        alma,
        atr,
        bollinger,
        dema,
        hma,
        hull_suite,
        linreg_slope,
        macd,
        roc,
        rsi,
        sma,
        smma,
        stochastic,
        supertrend,
        t3,
        tema,
        wma,
    )

    closes_only = lambda candles: [_to_float(c.ohlc.close.value) for c in candles]  # noqa: E731

    def _fn_sma(candles, length, source):
        return sma(_source_values(candles, source), int(length))

    def _fn_ema(candles, length, source):
        return _sma_seeded_ema(_source_values(candles, source), int(length))

    def _fn_wma(candles, length):
        return wma(closes_only(candles), int(length))

    def _fn_hma(candles, length, source):
        return hma(_source_values(candles, source), int(length))

    def _fn_dema(candles, length):
        return dema(closes_only(candles), int(length))

    def _fn_tema(candles, length):
        return tema(closes_only(candles), int(length))

    def _fn_alma(candles, length, offset, sigma):
        return alma(closes_only(candles), int(length), float(offset), float(sigma))

    def _fn_smma(candles, length, source):
        return {"smma": smma(_source_values(candles, source), int(length))}

    def _fn_t3(candles, length, factor, source, highlightMovements):
        return {"t3": t3(_source_values(candles, source), int(length), float(factor))}

    def _fn_linreg_slope(candles, periods):
        return {"slope": linreg_slope(closes_only(candles), int(periods))}

    def _fn_hull_suite(
        candles, source, mode, length, lengthMult, visualSwitch,
        switchColor, candleCol,
    ):
        return hull_suite(
            _source_values(candles, source), str(mode), int(length),
            float(lengthMult), bool(visualSwitch),
        )

    def _fn_rsi(candles, length, source):
        return rsi(_source_values(candles, source), int(length))

    def _fn_roc(candles, length):
        return roc(closes_only(candles), int(length))

    def _fn_macd(candles, fastPeriod, slowPeriod, signalPeriod, source):
        return macd(
            _source_values(candles, source), int(fastPeriod), int(slowPeriod), int(signalPeriod)
        )

    def _fn_stochastic(candles, kPeriod, kSmoothing, dPeriod):
        return stochastic(candles, int(kPeriod), int(kSmoothing), int(dPeriod))

    return [
        IndicatorSpec(
            id="sma", name="SMA", category="Trend", placement="overlay",
            params=(("length", "int", 20), ("source", "source", "close")),
            plots=(("value", "line", "SMA"),),
            fn=_fn_sma,
        ),
        IndicatorSpec(
            id="ema", name="EMA", category="Trend", placement="overlay",
            params=(("length", "int", 20), ("source", "source", "close")),
            plots=(("value", "line", "EMA"),),
            fn=_fn_ema,
        ),
        IndicatorSpec(
            id="wma", name="WMA", category="Trend", placement="overlay",
            params=(("length", "int", 20),),
            plots=(("value", "line", "WMA"),),
            fn=_fn_wma,
        ),
        IndicatorSpec(
            id="hma", name="HMA", category="Trend", placement="overlay",
            params=(("length", "int", 9), ("source", "source", "close")),
            plots=(("value", "line", "HMA"),),
            fn=_fn_hma,
        ),
        IndicatorSpec(
            id="dema", name="DEMA", category="Trend", placement="overlay",
            params=(("length", "int", 9),),
            plots=(("value", "line", "DEMA"),),
            fn=_fn_dema,
        ),
        IndicatorSpec(
            id="tema", name="TEMA", category="Trend", placement="overlay",
            params=(("length", "int", 9),),
            plots=(("value", "line", "TEMA"),),
            fn=_fn_tema,
        ),
        IndicatorSpec(
            id="alma", name="ALMA", category="Trend", placement="overlay",
            params=(("length", "int", 9), ("offset", "float", 0.85), ("sigma", "float", 6.0)),
            plots=(("value", "line", "ALMA"),),
            fn=_fn_alma,
        ),
        IndicatorSpec(
            id="smma", name="SMMA", category="Trend", placement="overlay",
            params=(("length", "int", 7), ("source", "source", "close")),
            plots=(("smma", "line", "SMMA"),),
            fn=_fn_smma,
        ),
        IndicatorSpec(
            id="t3", name="T3", category="Trend", placement="overlay",
            params=(
                ("length", "int", 5),
                ("factor", "float", 0.7),
                ("source", "source", "close"),
                ("highlightMovements", "bool", True),
            ),
            plots=(("t3", "line", "T3"),),
            fn=_fn_t3,
        ),
        IndicatorSpec(
            id="linreg-slope", name="Linear Regression Slope", category="Trend", placement="pane",
            params=(("periods", "int", 14),),
            plots=(("slope", "line", "Slope"),),
            levels=({"value": 0},),
            fn=_fn_linreg_slope,
        ),
        IndicatorSpec(
            id="hull-suite", name="Hull Suite", category="Trend", placement="overlay",
            params=(
                ("source", "source", "close"),
                ("mode", "select", "Hma"),
                ("length", "int", 55),
                ("lengthMult", "float", 1.0),
                ("visualSwitch", "bool", True),
                ("switchColor", "bool", True),
                ("candleCol", "bool", False),
            ),
            plots=(("mhull", "line", "Hull"), ("shull", "line", "Hull Displaced")),
            fn=_fn_hull_suite,
        ),
        IndicatorSpec(
            id="rsi", name="RSI", category="Momentum", placement="pane",
            params=(("length", "int", 14), ("source", "source", "close")),
            plots=(("value", "line", "RSI"),),
            levels=({"value": 70}, {"value": 30}),
            fn=_fn_rsi,
        ),
        IndicatorSpec(
            id="roc", name="ROC", category="Momentum", placement="pane",
            params=(("length", "int", 10),),
            plots=(("value", "line", "ROC"),),
            levels=({"value": 0},),
            fn=_fn_roc,
        ),
        IndicatorSpec(
            id="macd", name="MACD", category="Momentum", placement="pane",
            params=(
                ("fastPeriod", "int", 12),
                ("slowPeriod", "int", 26),
                ("signalPeriod", "int", 9),
                ("source", "source", "close"),
            ),
            plots=(
                ("macd", "line", "MACD"),
                ("signal", "line", "Signal"),
                ("histogram", "histogram", "Histogram"),
            ),
            levels=({"value": 0},),
            fn=_fn_macd,
        ),
        IndicatorSpec(
            id="bollinger", name="Bollinger Bands", category="Volatility",
            placement="overlay",
            params=(("length", "int", 20), ("stdDev", "float", 2.0), ("source", "source", "close")),
            plots=(
                ("upper", "line", "Upper"),
                ("middle", "line", "Middle"),
                ("lower", "line", "Lower"),
            ),
            fn=lambda candles, length, stdDev, source: bollinger(
                _source_values(candles, source), int(length), float(stdDev)
            ),
        ),
        IndicatorSpec(
            id="atr", name="ATR", category="Volatility", placement="pane",
            params=(("period", "int", 14),),
            plots=(("value", "line", "ATR"),),
            fn=atr,
        ),
        IndicatorSpec(
            id="vwap", name="VWAP (session)", category="Volume", placement="overlay",
            params=(
                ("anchor", "select", "session"),
                ("source", "source", "hlc3"),
                ("offset", "int", 0),
                ("calcMode", "select", "stdev"),
                ("showBand1", "bool", True),
                ("bandMult1", "float", 1.0),
                ("showBand2", "bool", False),
                ("bandMult2", "float", 2.0),
                ("showBand3", "bool", False),
                ("bandMult3", "float", 3.0),
            ),
            plots=(
                ("value", "line", "VWAP"),
                ("vwap", "line", "VWAP"),
                ("upper1", "line", "Upper Band #1"),
                ("lower1", "line", "Lower Band #1"),
                ("upper2", "line", "Upper Band #2"),
                ("lower2", "line", "Lower Band #2"),
                ("upper3", "line", "Upper Band #3"),
                ("lower3", "line", "Lower Band #3"),
            ),
            fn=_fn_vwap,
        ),
        IndicatorSpec(
            id="obv", name="OBV", category="Volume", placement="pane",
            params=(
                ("maType", "select", "None"),
                ("maLength", "int", 9),
                ("bbMult", "float", 2.0),
            ),
            plots=(
                ("value", "line", "OBV"),
                ("obv", "line", "OBV"),
                ("ma", "line", "OBV-based MA"),
                ("bbUpper", "line", "Upper Bollinger Band"),
                ("bbLower", "line", "Lower Bollinger Band"),
            ),
            fn=_fn_obv,
        ),
        IndicatorSpec(
            id="stochastic", name="Stochastic", category="Momentum", placement="pane",
            params=(("kPeriod", "int", 14), ("kSmoothing", "int", 3), ("dPeriod", "int", 3)),
            plots=(("k", "line", "%K"), ("d", "line", "%D"),),
            levels=({"value": 80}, {"value": 20}),
            fn=_fn_stochastic,
        ),
        IndicatorSpec(
            id="supertrend", name="Supertrend", category="Trend", placement="overlay",
            params=(("period", "int", 10), ("multiplier", "float", 3.0)),
            plots=(
                ("line", "line", "Supertrend"),
                ("up", "line", "Supertrend Up"),
                ("down", "line", "Supertrend Down"),
            ),
            fn=supertrend,
        ),
    ]
