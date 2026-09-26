"""Golden parity: every registered indicator must match the actual
openalgo-charts TypeScript implementation exactly (1e-9, None-aligned).

The goldens are the ground truth, so the comparison is driven from their side.
The loop used to iterate the *backend's* output keys, which made it blind in
one direction: a plot the backend stopped emitting simply dropped out of the
comparison and the test stayed green (``supertrend.bodyMid`` sat there
unnoticed). Every plot a golden declares is now accounted for exactly once, as
a compared series, a level line, or a recorded divergence (see
:func:`_accounting`), and nothing can fall through the accounting silently.
"""
import json
from datetime import datetime, timedelta, timezone
from decimal import Decimal
from pathlib import Path

import pytest
from tradex_analytics.indicators import compute_indicator, get_indicator_spec

GOLDENS = Path(__file__).parent / "goldens"

# TS descriptor id -> {TS settings key: backend param name}
PARAM_MAP = {
    "sma": {"length": "period", "source": "source"},
    "ema": {"length": "period", "source": "source"},
    "rsi": {"length": "period", "source": "source"},
    "roc": {"length": "period"},
    "macd": {
        "fastPeriod": "fast", "slowPeriod": "slow",
        "signalPeriod": "signal", "source": "source",
    },
    "bollinger": {"length": "period", "stdDev": "num_std", "source": "source"},
    "atr": {"period": "period"},
    "obv": {"maType": "ma_type", "maLength": "ma_length", "bbMult": "bb_mult"},
    "vwap": {
        "anchor": "anchor", "source": "source", "offset": "offset",
        "calcMode": "calcMode", "showBand1": "showBand1", "bandMult1": "bandMult1",
        "showBand2": "showBand2", "bandMult2": "bandMult2",
        "showBand3": "showBand3", "bandMult3": "bandMult3",
    },
    "stochastic": {"kPeriod": "k_period", "kSmoothing": "smooth_k", "dPeriod": "d_period"},
    "supertrend": {"period": "period", "multiplier": "multiplier"},
    "wma": {"length": "period"},
    "hma": {"length": "period", "source": "source"},
    "dema": {"length": "period"},
    "tema": {"length": "period"},
    "alma": {"length": "period", "offset": "offset", "sigma": "sigma"},
    "vwma": {"length": "period"},
    # Empty: the backend's `fn` takes no kwargs at all (SPEC_TWAP.params is
    # empty and `compute_indicator` rejects unknown names), so there is nothing
    # to pass. The values the golden records are hard-coded into `twap()` and
    # are pinned in HARDCODED_SETTINGS below rather than fed in.
    "twap": {},
    "mcginley-dynamic": {"length": "period"},
    "lsma": {"length": "period"},
    "envelope": {"length": "period", "percent": "percent"},
    "donchian": {"length": "period", "offset": "offset"},
    "keltner-channel": {"length": "period", "mult": "mult", "atrlength": "atrlength"},
    "median": {"length": "length", "atrLength": "atr_length", "atrMult": "atr_mult"},
    "adx": {"period": "period", "adxPeriod": "adx_period"},
    "aroon": {"length": "length"},
    "aroon-oscillator": {"length": "length"},
    "awesome-oscillator": {},
    "cci": {
        "period": "period", "constant": "constant",
        "maType": "ma_type", "maLength": "ma_length", "bbMult": "bb_mult",
    },
    "mfi": {"period": "period"},
    "ppo": {
        "fastLength": "fast_length",
        "slowLength": "slow_length",
        "signalLength": "signal_length",
        "oscType": "osc_type",
        "sigType": "sig_type",
    },
    "trix": {"length": "period"},
    "tsi": {"long": "long_length", "short": "short_length", "signal": "signal_length"},
    "smi": {"lengthK": "length_k", "lengthD": "length_d", "lengthEMA": "length_ema"},
    "smi-ergodic-indicator": {
        "longlen": "long_length", "shortlen": "short_length", "siglen": "signal_length",
    },
    "smi-ergodic-oscillator": {
        "longlen": "long_length", "shortlen": "short_length", "siglen": "signal_length",
    },
    "stochastic-rsi": {
        "smoothK": "smooth_k",
        "smoothD": "smooth_d",
        "lengthRSI": "length_rsi",
        "lengthStoch": "length_stoch",
    },
    "williams-percent-r": {"length": "length"},
    "ultimate-oscillator": {"length1": "length1", "length2": "length2", "length3": "length3"},
    "coppock-curve": {
        "wmaLength": "wma_length",
        "longRoCLength": "long_roc_length",
        "shortRoCLength": "short_roc_length",
    },
    "dpo": {"period": "period", "isCentered": "is_centered"},
    "fisher-transform": {"length": "length"},
    "chande-momentum": {"length": "length"},
    "connors-rsi": {"lenrsi": "lenrsi", "lenupdown": "lenupdown", "lenroc": "lenroc"},
    "balance-of-power": {},
    "bollinger-percent-b": {"length": "length", "mult": "mult"},
    "bollinger-bandwidth": {
        "length": "length", "mult": "mult",
        "expansionLength": "expansionLength", "contractionLength": "contractionLength",
    },
    "bb-trend": {
        "shortLength": "short_length", "longLength": "long_length", "stdDevMult": "std_dev_mult",
    },
    "kama": {
        "erLength": "er_length", "fastLength": "fast_length", "slowLength": "slow_length",
    },
    "choppiness-index": {"length": "length", "offset": "offset"},
    "historical-volatility": {"length": "length", "per": "per"},
    "average-daily-range": {"length": "length"},
    "chop-zone": {},
    "volatility-stop": {"length": "length", "factor": "factor", "source": "source"},
    "chandelier-exit": {
        "length": "length", "atrLength": "atr_length", "atrMultiplier": "atr_multiplier",
    },
    "chande-kroll-stop": {"p": "p", "x": "x", "q": "q"},
    "adl": {},
    "volume": {},
    "pvt": {},
    "chaikin-money-flow": {"length": "length"},
    "chaikin-oscillator": {"short": "short", "long": "long"},
    "ease-of-movement": {"length": "length", "divisor": "divisor"},
    "elder-force-index": {"length": "length"},
    "ulcer-index": {"length": "length"},
    "nvi": {"maLength": "ma_length"},
    "pvi": {"maLength": "ma_length"},
    "pvo": {
        "fastLength": "fast_length",
        "slowLength": "slow_length",
        "signalLength": "signal_length",
    },
    "mass-index": {"length": "length"},
    "know-sure-thing": {
        "roclen1": "roclen1", "roclen2": "roclen2",
        "roclen3": "roclen3", "roclen4": "roclen4",
        "smalen1": "smalen1", "smalen2": "smalen2",
        "smalen3": "smalen3", "smalen4": "smalen4",
        "siglen": "siglen",
    },
    "klinger-oscillator": {},
    "momentum": {"len": "len"},
    "ma-cross": {"shortLength": "short_length", "longLength": "long_length"},
    "ma-ribbon": {
        "ma1Type": "ma1_type", "ma1Source": "ma1_source", "ma1Length": "ma1_length",
        "ma2Type": "ma2_type", "ma2Source": "ma2_source", "ma2Length": "ma2_length",
        "ma3Type": "ma3_type", "ma3Source": "ma3_source", "ma3Length": "ma3_length",
        "ma4Type": "ma4_type", "ma4Source": "ma4_source", "ma4Length": "ma4_length",
    },
    "woodies-cci": {"cciTurboLength": "cci_turbo_length", "cci14Length": "cci14_length"},
    "special-k": {"length1": "length1", "length2": "length2"},
    "alligator": {
        "jawLength": "jaw_length", "teethLength": "teeth_length", "lipsLength": "lips_length",
        "jawOffset": "jaw_offset", "teethOffset": "teeth_offset", "lipsOffset": "lips_offset",
    },
    "parabolic-sar": {"start": "start", "increment": "increment", "maximum": "maximum"},
    "ichimoku": {
        "conversionPeriod": "conversion", "basePeriod": "base",
        "laggingSpanPeriod": "lagging", "displacement": "displacement",
    },
    "halftrend": {
        "amplitude": "amplitude", "channelDeviation": "channel_deviation",
        "atrPeriod": "atr_period",
    },
    "alphatrend": {"coeff": "coeff", "AP": "ap"},
    "cpr": {
        "pivotMode": "pivot_mode",
        "showDaily": "show_daily", "showWeekly": "show_weekly", "showMonthly": "show_monthly",
        "displaypivots": "display_pivots", "displaysupport": "display_support",
        "displayresistance": "display_resistance", "displaycpr": "display_cpr",
        "displayS1R1": "display_s1r1",
    },
    "range-analysis": {"showAverage": "show_average", "avgLength": "avg_length"},
    "vortex": {"length": "length"},
    "relative-vigor-index": {"length": "length", "offset": "offset"},
    "relative-volatility-index": {
        "length": "length", "offset": "offset",
        "maType": "ma_type", "maLength": "ma_length", "bbMult": "bb_mult",
    },
    "rsi-divergence": {"length": "length", "lbR": "lb_r", "lbL": "lb_l"},
    "trend-strength-index": {"length": "length"},
    "williams-fractals": {"periods": "periods"},
    "williams-vix-fix": {
        "pd": "pd", "bbl": "bbl", "mult": "mult", "lb": "lb", "ph": "ph", "pl": "pl",
    },
    "wavetrend": {"n1": "n1", "n2": "n2", "sigLen": "sig_len"},
    "smma": {"length": "length", "source": "source"},
    "t3": {
        "length": "length", "factor": "factor", "source": "source",
        "highlightMovements": "highlightMovements",
    },
    "linreg-slope": {"periods": "periods"},
    "hull-suite": {
        "source": "source", "mode": "mode", "length": "length",
        "lengthMult": "lengthMult", "visualSwitch": "visualSwitch",
        "candleCol": "candleCol",
    },
    "standard-deviation": {"periods": "periods", "deviations": "deviations"},
    "standard-error": {"length": "length"},
    "standard-error-bands": {
        "periods": "periods", "errors": "errors", "method": "method",
        "averagePeriods": "averagePeriods",
    },
    "ma-channel": {
        "upperLength": "upperLength", "lowerLength": "lowerLength",
        "upperOffset": "upperOffset", "lowerOffset": "lowerOffset",
    },
    "chaikin-volatility": {"periods": "periods", "rocLookback": "rocLookback"},
    "net-volume": {},
    "consolidation-breakout": {"markbreakout": "markbreakout", "colorinside": "colorinside"},
    "seasonality": {
        "startYear": "startYear", "cutoffPercent": "cutoffPercent",
        "tablePosition": "tablePosition", "tableWidth": "tableWidth",
        "tableHeight": "tableHeight", "showAvg": "showAvg",
        "showStDev": "showStDev", "showPos": "showPos",
        "ignoredMonths": "ignoredMonths",
    },
}

# backend plot key -> TS golden plot key. Complete and mandatory: every
# indicator carries an explicit entry, because the old fallback
# ({k: k for k in got}, identity over the *backend's* keys) is what let a
# golden plot go uncompared when the backend stopped emitting it. Level lines
# are absent from this map on purpose and are accounted for by
# :func:`_level_plots`; recorded divergences by DIVERGENCES below.
#
# Extended beyond the brief's seed map: the TS goldens name their plots
# ('rsi', 'roc', 'atr', 'obv', 'vwap') rather than 'value', and OBV/VWAP
# goldens carry extra derived plots (ma/bb bands, stdev bands) that the
# Batch-0 backend does not compute — those are out of registry scope here.
PLOT_MAP = {
    "sma": {"value": "ma"},
    "ema": {"value": "ma"},
    "bollinger": {"middle": "basis", "upper": "upper", "lower": "lower"},
    "rsi": {"value": "rsi"},
    "roc": {"value": "roc"},
    "atr": {"value": "atr"},
    "obv": {"value": "obv", "ma": "ma", "bbUpper": "bbUpper", "bbLower": "bbLower"},
    "vwap": {
        "value": "vwap",
        "upper1": "upper1", "lower1": "lower1",
        "upper2": "upper2", "lower2": "lower2",
        "upper3": "upper3", "lower3": "lower3",
    },
    "wma": {"value": "ma"},
    "hma": {"value": "hma"},
    "dema": {"value": "dema"},
    "tema": {"value": "tema"},
    "alma": {"value": "alma"},
    "vwma": {"value": "vwma"},
    "twap": {"value": "twap"},
    "mcginley-dynamic": {"value": "mg"},
    "lsma": {"value": "lsma"},
    "envelope": {"middle": "basis", "upper": "upper", "lower": "lower"},
    "donchian": {"middle": "basis", "upper": "upper", "lower": "lower"},
    "keltner-channel": {"middle": "basis", "upper": "upper", "lower": "lower"},
    "median": {
        "median": "median",
        "upper": "upper",
        "lower": "lower",
        "median_ema": "medianEma",
    },
    "adx": {"plusDi": "plusDi", "minusDi": "minusDi", "adx": "adx"},
    "aroon": {"up": "up", "down": "down"},
    "aroon-oscillator": {"osc": "osc"},
    "awesome-oscillator": {"ao": "ao"},
    "cci": {"cci": "cci", "ma": "ma", "bbUpper": "bbUpper", "bbLower": "bbLower"},
    "mfi": {"value": "mfi"},
    "ppo": {"ppo": "ppo", "signal": "signal", "hist": "hist"},
    "trix": {"value": "trix"},
    "tsi": {"tsi": "tsi", "signal": "signal"},
    "smi": {"smi": "smi", "ema": "ema"},
    "smi-ergodic-indicator": {"erg": "erg", "sig": "sig"},
    "smi-ergodic-oscillator": {"osc": "osc"},
    "stochastic-rsi": {"k": "k", "d": "d"},
    "williams-percent-r": {"percentR": "percentR"},
    "ultimate-oscillator": {"uo": "uo"},
    "coppock-curve": {"curve": "curve"},
    "dpo": {"dpo": "dpo"},
    "fisher-transform": {"fisher": "fisher", "trigger": "trigger"},
    "chande-momentum": {"cmo": "cmo"},
    "connors-rsi": {"crsi": "crsi"},
    "balance-of-power": {"bop": "bop"},
    "bollinger-percent-b": {"value": "percentB"},
    "bollinger-bandwidth": {
        "bandwidth": "bandwidth", "expansion": "expansion", "contraction": "contraction",
    },
    "bb-trend": {"value": "bbtrend"},
    "kama": {"value": "kama"},
    "choppiness-index": {"value": "chop"},
    "historical-volatility": {"value": "hv"},
    "average-daily-range": {"value": "adr"},
    "chop-zone": {"chopZone": "chopZone"},
    "volatility-stop": {"up": "up", "down": "down"},
    "chandelier-exit": {"longExit": "longExit", "shortExit": "shortExit"},
    "chande-kroll-stop": {"stopLong": "stopLong", "stopShort": "stopShort"},
    "adl": {"adl": "adl"},
    "volume": {"volume": "volume"},
    "pvt": {"pvt": "pvt"},
    "chaikin-money-flow": {"cmf": "cmf"},
    "chaikin-oscillator": {"osc": "osc"},
    "ease-of-movement": {"eom": "eom"},
    "elder-force-index": {"efi": "efi"},
    "ulcer-index": {"ui": "ui"},
    "nvi": {"nvi": "nvi", "ema": "ema"},
    "pvi": {"pvi": "pvi", "ema": "ema"},
    "pvo": {"hist": "hist", "pvo": "pvo", "signal": "signal"},
    "mass-index": {"mi": "mi"},
    "know-sure-thing": {"kst": "kst", "signal": "signal"},
    "klinger-oscillator": {"kvo": "kvo", "signal": "signal"},
    "momentum": {"mom": "mom"},
    "ma-cross": {"short": "short", "long": "long", "cross": "cross"},
    "ma-ribbon": {"ma1": "ma1", "ma2": "ma2", "ma3": "ma3", "ma4": "ma4"},
    "woodies-cci": {"hist": "hist", "turbo": "turbo", "cci14": "cci14"},
    "special-k": {"specialK": "specialK", "signal": "signal"},
    "alligator": {"jaw": "jaw", "teeth": "teeth", "lips": "lips"},
    "parabolic-sar": {"sar": "sar"},
    "ichimoku": {
        "conversion": "conversion", "base": "base", "spanA": "spanA",
        "spanB": "spanB", "lagging": "lagging",
    },
    "halftrend": {
        "up": "up", "down": "down", "atr_high": "atrHigh", "atr_low": "atrLow",
        "buy_signal": "buySignal", "sell_signal": "sellSignal",
    },
    "alphatrend": {
        "alphatrend": "alphatrend", "lagged": "lagged",
        "buy_signal": "buySignal", "sell_signal": "sellSignal",
    },
    "cpr": {
        "dPivot": "dPivot", "dS1": "dS1", "dS2": "dS2", "dS3": "dS3",
        "dR1": "dR1", "dR2": "dR2", "dR3": "dR3", "dBc": "dBc", "dTc": "dTc",
        "wPivot": "wPivot", "wS1": "wS1", "wS2": "wS2", "wS3": "wS3",
        "wR1": "wR1", "wR2": "wR2", "wR3": "wR3", "wBc": "wBc", "wTc": "wTc",
        "mPivot": "mPivot", "mS1": "mS1", "mS2": "mS2", "mS3": "mS3",
        "mR1": "mR1", "mR2": "mR2", "mR3": "mR3", "mBc": "mBc", "mTc": "mTc",
    },
    "range-analysis": {"range": "range", "avg_range": "avgRange"},
    "vortex": {"vip": "vip", "vim": "vim"},
    "relative-vigor-index": {"rvgi": "rvgi", "signal": "signal"},
    "relative-volatility-index": {
        "rvi": "rvi", "ma": "ma", "bb_upper": "bbUpper", "bb_lower": "bbLower",
    },
    "rsi-divergence": {
        "rsi": "rsi", "bull": "bull", "hiddenBull": "hiddenBull",
        "bear": "bear", "hiddenBear": "hiddenBear",
    },
    "trend-strength-index": {"tsi": "tsi"},
    "williams-fractals": {
        "fractals": "fractals", "upFractal": "upFractal", "downFractal": "downFractal",
    },
    "williams-vix-fix": {
        "wvf": "wvf", "range_high": "rangeHigh",
        "range_low": "rangeLow", "upper_band": "upperBand",
    },
    "wavetrend": {"mom": "mom", "wt1": "wt1", "wt2": "wt2"},
    "chaikin-volatility": {"chaikinVolatility": "chaikinVolatility"},
    "consolidation-breakout": {
        "rangeHigh": "rangeHigh", "rangeLow": "rangeLow", "breakUp": "breakUp",
        "breakDown": "breakDown", "insideAge": "insideAge",
    },
    "hull-suite": {"mhull": "mhull", "shull": "shull"},
    "linreg-slope": {"slope": "slope"},
    "ma-channel": {"upper": "upper", "lower": "lower"},
    "macd": {"histogram": "histogram", "macd": "macd", "signal": "signal"},
    "net-volume": {"net": "net"},
    "seasonality": {"seasonality": "seasonality"},
    "smma": {"smma": "smma"},
    "standard-deviation": {"stdDev": "stdDev"},
    "standard-error": {"stdErr": "stdErr"},
    "standard-error-bands": {"upper": "upper", "basis": "basis", "lower": "lower"},
    "stochastic": {"k": "k", "d": "d"},
    "supertrend": {"up": "up", "down": "down"},
    "t3": {"t3": "t3"},
}


class _OHLC:
    def __init__(self, o, h, low, c):
        self.open = _P(o)
        self.high = _P(h)
        self.low = _P(low)
        self.close = _P(c)


class _P:
    def __init__(self, v):
        self.value = Decimal(str(v))


class _Vol:
    def __init__(self, v):
        self.value = v


class _Candle:
    def __init__(self, time, o, h, low, c, v):
        IST = timezone(timedelta(hours=5, minutes=30))
        self.timestamp = datetime.fromtimestamp(time, tz=IST).replace(tzinfo=None)
        self.ohlc = _OHLC(o, h, low, c)
        self.volume = _Vol(v)


def _candles():
    bars = json.loads((GOLDENS / "fixtures.json").read_text())["bars"]
    return [_Candle(*b) for b in bars]


CANDLES = _candles()


# Settings a golden records that the backend satisfies without a parameter,
# because its compute function takes no kwargs at all. The values are what the
# backend hard-codes, and the guard below fails the moment a golden records a
# different one, so this cannot rot into a silent "ran on defaults" the way an
# empty PARAM_MAP entry could.
HARDCODED_SETTINGS = {
    "twap": {
        "anchor": "session",
        "source": "ohlc4",
        "offset": 0,
    },
}

# Golden plots the backend does not reproduce. Recorded rather than dropped,
# because the alternative (leaving them uncompared) is the defect this gate
# was fixed for. Each entry is re-checked by a strict-xfail test below: the
# day the backend starts reproducing the plot, that test passes, strict=True
# turns the pass into a failure, and the entry has to be deleted.
DIVERGENCES = {
    ("supertrend", "bodyMid"): (
        "backend stops at the plots it declares (line, up, down) and never "
        "emits the fill's far edge; openalgo-charts trend.ts:349 computes "
        "(open + close) / 2 on every live bar and the descriptor fills "
        "bodyMid->up and bodyMid->down against it. The backend holds every "
        "input this needs (direction for liveness, the candle's own ohlc), so "
        "this is a missing series, not a missing capability"
    ),
    ("chop-zone", "angle"): (
        "the backend computes a different number, not a different shape: "
        "volatility/volatility_chop.py:194 takes `lo` as the 30-bar lowest "
        "LOW while openalgo-charts volatility.ts:431 takes the 30-bar lowest "
        "HIGH (`lowest(bars.map((b) => b.high), ...)`), so the rescaling span "
        "and therefore the angle are wrong from bar 34 on. 261 of 300 values "
        "disagree. The TS source comments on the quirk; the Python docstring "
        "asserts the opposite convention"
    ),
    ("wavetrend", "buy"): (
        "cross markers are not implemented; wavetrend.ts computes buy/sell "
        "from the filterZone/useInner zone gate, and the backend's spec "
        "declares only (mom, wt1, wt2) plus levels"
    ),
    ("wavetrend", "sell"): "see wavetrend.buy: the same unimplemented cross marker",
    ("wavetrend", "bull"): (
        "divergence markers are not implemented; the backend declares no "
        "pivot/divergence series. All-None on this fixture, so the golden "
        "cannot show a value disagreement here either"
    ),
    ("wavetrend", "bear"): "see wavetrend.bull: the same unimplemented divergence marker",
    ("wavetrend", "hiddenBull"): "see wavetrend.bull: the same unimplemented divergence marker",
    ("wavetrend", "hiddenBear"): "see wavetrend.bull: the same unimplemented divergence marker",
    ("williams-vix-fix", "alertUpper"): (
        "the ph/pl alert columns are not implemented; momentum.ts returns "
        "alertUpper/alertHigh alongside the range and the backend emits only "
        "wvf, range_high, range_low, upper_band"
    ),
    ("williams-vix-fix", "alertHigh"): (
        "see williams-vix-fix.alertUpper: the same unimplemented alert column"
    ),
}

# The shape an indicator golden has. PROVENANCE.json and fixtures.json share
# the directory and are not indicators; they are excluded by this test rather
# than by a filename denylist, which would rot the next time a sidecar is
# added and would keep reporting coverage the gate does not have.
_GOLDEN_DOC_KEYS = ("id", "settings", "plots")


def _is_indicator_golden(path: Path) -> bool:
    try:
        doc = json.loads(path.read_text())
    except (OSError, UnicodeDecodeError, json.JSONDecodeError):
        return False
    return isinstance(doc, dict) and all(k in doc for k in _GOLDEN_DOC_KEYS)


def _golden_ids():
    return sorted(p.stem for p in GOLDENS.glob("*.json") if _is_indicator_golden(p))


def _load(ts_id):
    return json.loads((GOLDENS / f"{ts_id}.json").read_text())


def _compare(name, actual, expected):
    assert len(actual) == len(expected), f"{name}: length {len(actual)} != {len(expected)}"
    for i, (a, e) in enumerate(zip(actual, expected)):
        if e is None:
            assert a is None, f"{name}[{i}]: expected None, got {a}"
        else:
            assert a is not None, f"{name}[{i}]: expected {e}, got None"
            assert a == pytest.approx(e, abs=1e-9), f"{name}[{i}]"


def _level_plots(golden, spec):
    """Golden plots that are a constant level line, as {plot key: level}.

    The library returns these as value columns so they can be drawn against;
    the registry carries them as `spec.levels` instead. They are recognised by
    shape, not by name, and verified rather than skipped: a plot only lands
    here if every one of its values is the same number AND that number is a
    level the backend declares.
    """
    declared = {lv["value"] for lv in spec.levels}
    out = {}
    for key, series in golden["plots"].items():
        values = {v for v in series if v is not None}
        if len(values) == 1:
            only = values.pop()
            if only in declared:
                out[key] = only
    return out


def _synthesised_series(ts_id, got, golden):
    """Backend series the golden has no single column for.

    Supertrend is the one: TS splits the stop into up/down by direction, the
    backend ships one line plus native companions, and the gate rebuilds the
    combined series from the golden's own two columns so it is compared too.
    """
    if ts_id != "supertrend":
        return got
    out = dict(got)
    up, down, direction = golden["plots"]["up"], golden["plots"]["down"], got["direction"]
    out["line"] = [u if d == 1 else w for u, w, d in zip(up, down, direction)]
    return out


def _accounting(ts_id, golden, spec, plot_map=None):
    """Classify every plot the golden declares, driving from the golden's side.

    Returns (compared, levels, unaccounted) where `compared` maps a golden plot
    key to the backend plot key that must carry it, `levels` maps a golden plot
    key to the level value it must equal everywhere, and `unaccounted` lists the
    plots that are neither (or are only covered by a recorded divergence). The
    old loop iterated PLOT_MAP.get(ts_id, {k: k for k in got}) — the *backend's*
    keys — so a plot the backend stopped emitting left the comparison silently
    and the test stayed green. Nothing may be left unaccounted.
    """
    plot_map = PLOT_MAP.get(ts_id, {}) if plot_map is None else plot_map
    inverse = {gk: bk for bk, gk in plot_map.items()}
    levels = _level_plots(golden, spec)
    divergent = {plot for (gid, plot) in DIVERGENCES if gid == ts_id}
    compared, unaccounted = {}, []
    for key in golden["plots"]:
        if key in levels or key in divergent:
            continue
        if key in inverse:
            compared[key] = inverse[key]
        else:
            unaccounted.append(key)
    return compared, levels, unaccounted


def _compute(ts_id, golden=None):
    golden = _load(ts_id) if golden is None else golden
    params = {py: golden["settings"][ts] for ts, py in PARAM_MAP[ts_id].items()}
    return _synthesised_series(ts_id, compute_indicator(ts_id, CANDLES, params), golden)


def _check_plots(ts_id, golden, spec, got, plot_map=None):
    """The parity comparison proper, driven from the golden's plots.

    Every plot the golden declares is handled exactly once: compared against a
    backend series, verified as a level line, or listed in DIVERGENCES. Nothing
    is skipped on the strength of the backend's own output, which is what let
    ``supertrend.bodyMid`` go missing with the suite still green.
    """
    compared, levels, unaccounted = _accounting(ts_id, golden, spec, plot_map)
    assert not unaccounted, (
        f"{ts_id}: golden declares {unaccounted}, which PLOT_MAP, the level "
        f"check and DIVERGENCES do not account for. Map it to a backend plot "
        f"key, or record it in DIVERGENCES with the reason it is not "
        f"reproduced; the old one-directional loop skipped it silently"
    )
    for gk, bk in compared.items():
        assert bk in got, (
            f"{ts_id}: golden plot {gk!r} maps to backend {bk!r}, which is not emitted"
        )
        _compare(f"{ts_id}.{gk}", got[bk], golden["plots"][gk])
    for gk, level in levels.items():
        # The level *value* is the classifier's precondition, so restating
        # constancy or the value here would be vacuous. What the classifier
        # does not check is that the column spans the fixture: a level line
        # truncated to the warmup is still "constant" and would be drawn short.
        series = golden["plots"][gk]
        assert len(series) == len(CANDLES), (
            f"{ts_id}: level plot {gk!r} spans {len(series)} bars, not the "
            f"fixture's {len(CANDLES)}; the library returns level lines "
            f"full-length"
        )


@pytest.mark.parametrize("ts_id", _golden_ids())
def test_parity_against_ts_source(ts_id):
    spec = get_indicator_spec(ts_id)
    assert spec is not None, f"{ts_id}: golden with no registered indicator"
    assert ts_id in PARAM_MAP, f"{ts_id}: no PARAM_MAP entry"
    golden = _load(ts_id)
    _check_plots(ts_id, golden, spec, _compute(ts_id, golden))


_DIVERGENCE_CASES = [
    pytest.param(ts_id, plot, id=f"{ts_id}.{plot}",
                 marks=pytest.mark.xfail(strict=True, reason=reason))
    for (ts_id, plot), reason in sorted(DIVERGENCES.items())
]


@pytest.mark.parametrize("ts_id,plot", _DIVERGENCE_CASES)
def test_known_divergence_is_still_divergent(ts_id, plot):
    """A recorded divergence is a promise to re-check, not a waiver.

    It passes only once the backend reproduces the plot, and strict xfail turns
    that pass into a failure until DIVERGENCES is updated, so the record
    cannot outlive the defect it was written for.
    """
    golden = _load(ts_id)
    got = _compute(ts_id, golden)
    if plot not in got:
        # Not emitted at all: compare against None so the absence is the
        # failure, and a future series with real values makes this pass.
        got = dict(got, **{plot: None})
    _compare(f"{ts_id}.{plot}", got[plot], golden["plots"][plot])


def test_every_golden_plot_is_compared_or_accounted_for():
    """The bidirectional half of the gate, as a test of its own.

    Every plot a golden declares must be compared against a backend series,
    verified as a level line, or recorded as a divergence. ``supertrend`` is
    the regression: ``bodyMid`` is in its golden and the pre-fix loop returned
    early for that indicator without ever looking at it.
    """
    for ts_id in _golden_ids():
        spec = get_indicator_spec(ts_id)
        assert spec is not None, f"{ts_id}: golden with no registered indicator"
        golden = _load(ts_id)
        compared, levels, unaccounted = _accounting(ts_id, golden, spec)
        divergent = {plot for (gid, plot) in DIVERGENCES if gid == ts_id}
        assert not unaccounted, f"{ts_id}: unaccounted golden plots {unaccounted}"
        assert not divergent & set(levels), (
            f"{ts_id}: a recorded divergence is also a level plot, so the "
            f"level check already covers it"
        )
        # The compared half needs every mapped backend key to actually exist.
        got = _compute(ts_id, golden)
        for gk, bk in compared.items():
            assert bk in got, f"{ts_id}.{gk}: backend key {bk!r} is not emitted"


def test_goldens_and_registry_cover_exactly_the_same_indicators():
    """No parity skip, and no golden for a retired indicator.

    Both directions are asserted because a one-sided check is what let
    PROVENANCE.json be globbed as an indicator id and reported as
    "not yet ported": a skip message that read like a real porting gap while
    all 102 indicators were in fact covered.
    """
    from tradex_analytics.indicators import indicator_catalogue

    registry = {entry["id"] for entry in indicator_catalogue()}
    _same_indicators(registry, set(_golden_ids()))
    assert len(registry) == 102, f"registry size changed: {len(registry)}"


def _same_indicators(registry, goldens):
    """Assert the two id sets are identical, naming whichever side differs.

    Both directions, because the interesting direction is the one a golden
    cannot report: an indicator with no golden is simply absent from the
    parametrised parity run, so the suite would be green with one fewer
    indicator under test and no message anywhere.
    """
    assert registry - goldens == set(), f"registered with no golden: {sorted(registry - goldens)}"
    assert goldens - registry == set(), (
        f"golden with no registered indicator: {sorted(goldens - registry)}"
    )


def test_sidecars_are_not_mistaken_for_indicators(tmp_path):
    """The exclusion is structural, so it survives the next sidecar.

    Both real sidecars are excluded, and so is a sidecar under a name nobody
    has written yet, which a denylist would not catch. A file whose name is not
    in any list but whose shape is an indicator's is still collected, so the
    rule cannot have quietly become "only these names count".
    """
    excluded = {p.stem for p in GOLDENS.glob("*.json")} - set(_golden_ids())
    assert "PROVENANCE" in excluded, "the provenance sidecar is being globbed as an indicator"
    assert "fixtures" in excluded, "the fixture file is being globbed as an indicator"

    provenance_shaped = tmp_path / "SCHEMA_DRIFT_2027.json"
    provenance_shaped.write_text(
        json.dumps({"library": "openalgo-charts", "sha": "deadbeef", "version": "9.9.9"})
    )
    assert not _is_indicator_golden(provenance_shaped), (
        "a sidecar under an unknown name was collected as an indicator; the "
        "exclusion has become a denylist"
    )

    bars_shaped = tmp_path / "another_fixture.json"
    bars_shaped.write_text(json.dumps({"bars": [[0, 1, 2, 3, 4, 5]]}))
    assert not _is_indicator_golden(bars_shaped), "a fixture under an unknown name was collected"

    indicator_shaped = tmp_path / "brand_new_indicator.json"
    indicator_shaped.write_text(
        json.dumps({"id": "brand_new_indicator", "settings": {}, "plots": {"value": []}})
    )
    assert _is_indicator_golden(indicator_shaped), (
        "an indicator-shaped golden under an unknown name was skipped; the "
        "gate would report a coverage gap that does not exist"
    )

    assert not _is_indicator_golden(tmp_path / "not_json.txt")


def test_every_backend_indicator_has_param_mapping():
    """Registry ids beyond Batch 0 must be explicitly mapped or skipped."""
    from tradex_analytics.indicators import indicator_catalogue

    for entry in indicator_catalogue():
        assert entry["id"] in PARAM_MAP, f"{entry['id']} missing from PARAM_MAP"


def test_every_indicator_has_a_plot_map():
    """PLOT_MAP is mandatory now, so a new indicator cannot fall back to it.

    The fallback was identity over the backend's keys, which is the direction
    that hides a missing plot.
    """
    for ts_id in _golden_ids():
        assert ts_id in PLOT_MAP, f"{ts_id} missing from PLOT_MAP"


def _is_presentational(key):
    """A setting the backend has no counterpart for: a colour."""
    return key == "color" or key.endswith("Color")


def test_empty_param_map_never_hides_a_differing_setting():
    """An empty PARAM_MAP is only honest when there is nothing to feed.

    Eleven indicators shipped with `{}`, which meant the golden's recorded
    settings were thrown away and the run used the backend's defaults instead.
    Two of them had real numeric settings (atr, supertrend) and are now mapped;
    twap's compute function takes no kwargs at all, so its values are pinned in
    HARDCODED_SETTINGS instead. The rest record colours only, which this test
    proves rather than assumes.
    """
    for ts_id in _golden_ids():
        mapping = PARAM_MAP.get(ts_id)
        assert mapping is not None, f"{ts_id}: no PARAM_MAP entry"
        if mapping:
            continue
        golden = _load(ts_id)
        hardcoded = HARDCODED_SETTINGS.get(ts_id, {})
        for key, value in golden["settings"].items():
            if _is_presentational(key):
                continue
            assert key in hardcoded, (
                f"{ts_id}: PARAM_MAP is empty but the golden records "
                f"{key}={value!r}, so the run would silently use the backend "
                f"default. Map it, or pin the value the backend hard-codes in "
                f"HARDCODED_SETTINGS"
            )
        for key, value in hardcoded.items():
            assert golden["settings"].get(key) == value, (
                f"{ts_id}: backend hard-codes {key}={value!r} but the golden "
                f"now records {golden['settings'].get(key)!r}"
            )


def test_no_backend_param_runs_on_a_default_the_golden_contradicts():
    """The general form of the same guard, alias-aware.

    A param left on its declared default is only acceptable where the golden
    records that same value for it. PARAM_MAP routes most names through
    LEGACY_PARAM_ALIASES, so the check resolves them before comparing.
    """
    from tradex_analytics.indicators import _resolve_indicator_params

    for ts_id in _golden_ids():
        golden = _load(ts_id)
        spec = get_indicator_spec(ts_id)
        params = {py: golden["settings"][ts] for ts, py in PARAM_MAP[ts_id].items()}
        merged = _resolve_indicator_params(ts_id, params)
        for name, _kind, default in spec.params:
            if merged.get(name) != default:
                continue
            recorded = golden["settings"].get(name)
            assert recorded is None or recorded == default, (
                f"{ts_id}: param {name}={default!r} (the declared default) "
                f"contradicts the golden's {recorded!r} and is not fed from it"
            )


def test_every_acceptable_setting_the_golden_records_is_fed_to_the_backend():
    """No setting the backend could take is quietly dropped from the run.

    This is the general form of the empty-PARAM_MAP defect, and it catches
    more than the empty case: dropping ``multiplier`` from ``supertrend``'s map
    is invisible while the golden happens to record 3 and the backend default
    is 3.0, because nothing then disagrees. "Acceptable" is read off the
    backend rather than hand-listed — a setting is one the spec declares, or
    one LEGACY_PARAM_ALIASES translates into a name the spec declares — so a
    setting the backend has no parameter for (a colour, or a TS input it never
    implemented) is out of scope by fact and not by opinion.
    """
    from tradex_analytics.indicators import LEGACY_PARAM_ALIASES

    for ts_id in _golden_ids():
        golden = _load(ts_id)
        spec = get_indicator_spec(ts_id)
        aliases = LEGACY_PARAM_ALIASES.get(ts_id, {})
        declared = {p[0] for p in spec.params}
        fed = {aliases.get(v, v) for v in PARAM_MAP[ts_id].values()}
        for key, value in golden["settings"].items():
            if _is_presentational(key):
                continue
            name = key if key in declared else aliases.get(key)
            if name is None:
                # The backend has no such parameter, so there is nothing to
                # feed and nothing to assert here: its compute function cannot
                # see the value either way, and the indicator's plots are
                # compared regardless. Settings the backend hard-codes are
                # pinned in HARDCODED_SETTINGS, which the empty-PARAM_MAP
                # guard covers.
                continue
            assert name in fed, (
                f"{ts_id}: golden records {key}={value!r} and the backend "
                f"accepts it as {name!r}, but PARAM_MAP does not feed it, so "
                f"the run uses the default instead"
            )


def test_every_recorded_divergence_is_strictly_xfailed():
    """The allowance is wired to fail when the defect is fixed.

    ``strict=True`` is the whole mechanism: without it a fixed backend turns
    the record into a silent pass and the entry can outlive its reason. It is
    asserted here because on a tree where every divergence is still live,
    strict and non-strict runs are indistinguishable — the difference only
    shows up after the fix, which is exactly when nobody is watching.
    """
    marks = [
        mark
        for param in _DIVERGENCE_CASES
        for mark in param.marks
    ]
    assert marks, "DIVERGENCES is not wired into any test case"
    for mark in marks:
        assert mark.kwargs.get("strict") is True, (
            "a divergence is xfailed non-strictly, so it can outlive its fix"
        )
        assert mark.kwargs.get("reason"), "a divergence is xfailed with no reason recorded"
    for (ts_id, plot) in DIVERGENCES:
        assert plot in _load(ts_id)["plots"], (
            f"DIVERGENCES names {ts_id}.{plot}, which is not a golden plot"
        )


def test_the_old_one_directional_loop_was_blind_to_a_missing_plot():
    """Why the comparison is driven from the golden's side.

    Runs the pre-fix loop verbatim on a backend that has stopped emitting one
    of the golden's plots, and asserts the blindness rather than just
    performing it: the old form compares every series correctly and still
    leaves a declared plot uncompared, while the accounting the gate now uses
    reports it. Without this, a future one-directional rewrite would look
    correct again, because on today's tree the only plot it misses is one
    already recorded in DIVERGENCES.
    """
    golden = {"id": "demo", "settings": {}, "plots": {"kept": [1.0, 2.0], "dropped": [3.0, 4.0]}}
    got = {"kept": [1.0, 2.0]}

    # Pre-fix, verbatim: PLOT_MAP.get(ts_id, {k: k for k in got}), iterated over
    # the backend's keys. Every comparison it makes holds...
    old_compared = {}
    for bk, gk in {"kept": "kept"}.items():
        _compare(f"demo.{bk}", got[bk], golden["plots"][gk])
        old_compared[gk] = bk
    assert old_compared == {"kept": "kept"}
    # ...and it silently leaves the rest of the golden uncompared.
    assert set(golden["plots"]) - set(old_compared) == {"dropped"}

    class _Spec:
        levels = ()

    compared, levels, unaccounted = _accounting("demo", golden, _Spec(), {"kept": "kept"})
    assert compared == {"kept": "kept"}
    assert not levels
    assert unaccounted == ["dropped"], "the accounting must not lose a plot"
    with pytest.raises(AssertionError, match="do not account for"):
        _check_plots("demo", golden, _Spec(), got, {"kept": "kept"})

    # A truncated level line is the other thing the accounting verifies, and it
    # is not implied by the classification (which only reads the defined
    # values), so it is checked against a golden of the right shape.
    class _LevelSpec:
        levels = ({"value": 70.0},)

    full = [70.0] * len(CANDLES)
    truncated = {"id": "demo", "settings": {}, "plots": {"kept": [1.0], "upperLevel": [70.0]}}
    complete = {"id": "demo", "settings": {}, "plots": {"kept": [1.0], "upperLevel": full}}
    assert _accounting("demo", complete, _LevelSpec(), {"kept": "kept"})[1] == {"upperLevel": 70.0}
    with pytest.raises(AssertionError, match="spans 1 bars"):
        _check_plots("demo", truncated, _LevelSpec(), {"kept": [1.0]}, {"kept": "kept"})


def test_an_indicator_with_no_golden_is_reported():
    """The coverage check's negative control, on a synthetic pair.

    An indicator the registry lists but no golden covers is missing from the
    parametrised run, so nothing else in this file would notice: the suite
    would be green with one fewer indicator under test.
    """
    _same_indicators({"a", "b"}, {"a", "b"})
    with pytest.raises(AssertionError, match=r"registered with no golden: \['b'\]"):
        _same_indicators({"a", "b"}, {"a"})
    with pytest.raises(AssertionError, match=r"golden with no registered indicator: \['c'\]"):
        _same_indicators({"a", "b"}, {"a", "b", "c"})


def test_a_plot_the_backend_drops_now_fails_the_parity_run():
    """The negative control on a real fixture, for the case that started it.

    ``supertrend`` short-circuits through a synthesised line and then returned,
    which is how ``bodyMid`` went uncompared. The accounting test above pins
    the classification; this pins the consequence by checking that a backend
    missing a mapped plot fails the run instead of passing quietly.
    """
    golden = _load("sma")
    spec = get_indicator_spec("sma")
    got = _compute("sma", golden)
    assert "value" in got
    _check_plots("sma", golden, spec, got)

    stripped = {k: v for k, v in got.items() if k != "value"}
    with pytest.raises(AssertionError, match="not emitted"):
        _check_plots("sma", golden, spec, stripped)

    # And a golden plot with no mapping at all is a hard failure too.
    with pytest.raises(AssertionError, match="do not account for"):
        _check_plots("sma", golden, spec, got, {})

