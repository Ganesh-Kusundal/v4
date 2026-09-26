"""Golden parity: every registered indicator must match the actual
openalgo-charts TypeScript implementation exactly (1e-9, None-aligned)."""
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
    "macd": {"fastPeriod": "fast", "slowPeriod": "slow", "signalPeriod": "signal", "source": "source"},
    "bollinger": {"length": "period", "stdDev": "num_std", "source": "source"},
    "atr": {},
    "obv": {"maType": "ma_type", "maLength": "ma_length", "bbMult": "bb_mult"},
    "vwap": {"anchor": "anchor", "source": "source", "offset": "offset", "calcMode": "calcMode", "showBand1": "showBand1", "bandMult1": "bandMult1", "showBand2": "showBand2", "bandMult2": "bandMult2", "showBand3": "showBand3", "bandMult3": "bandMult3"},
    "stochastic": {"kPeriod": "k_period", "kSmoothing": "smooth_k", "dPeriod": "d_period"},
    "supertrend": {},
    "wma": {"length": "period"},
    "hma": {"length": "period", "source": "source"},
    "dema": {"length": "period"},
    "tema": {"length": "period"},
    "alma": {"length": "period", "offset": "offset", "sigma": "sigma"},
    "vwma": {"length": "period"},
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
    "cci": {"period": "period", "constant": "constant", "maType": "ma_type", "maLength": "ma_length", "bbMult": "bb_mult"},
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
    "smi-ergodic-indicator": {"longlen": "long_length", "shortlen": "short_length", "siglen": "signal_length"},
    "smi-ergodic-oscillator": {"longlen": "long_length", "shortlen": "short_length", "siglen": "signal_length"},
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
    "bollinger-bandwidth": {"length": "length", "mult": "mult"},
    "bb-trend": {"shortLength": "short_length", "longLength": "long_length", "stdDevMult": "std_dev_mult"},
    "kama": {"erLength": "er_length", "fastLength": "fast_length", "slowLength": "slow_length"},
    "choppiness-index": {"length": "length", "offset": "offset"},
    "historical-volatility": {"length": "length", "per": "per"},
    "average-daily-range": {"length": "length"},
    "chop-zone": {},
    "volatility-stop": {"length": "length", "factor": "factor"},
    "chandelier-exit": {"length": "length", "atrLength": "atr_length", "atrMultiplier": "atr_multiplier"},
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
    "t3": {"length": "length", "factor": "factor", "source": "source"},
    "linreg-slope": {"periods": "periods"},
    "hull-suite": {
        "source": "source", "mode": "mode", "length": "length",
        "lengthMult": "lengthMult", "visualSwitch": "visualSwitch",
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

# backend plot key -> TS golden plot key (identity when absent).
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
    "vwap": {"value": "vwap", "upper1": "upper1", "lower1": "lower1", "upper2": "upper2", "lower2": "lower2", "upper3": "upper3", "lower3": "lower3"},
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
    "bollinger-bandwidth": {"bandwidth": "bandwidth"},
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
    "alphatrend": {"alphatrend": "alphatrend", "lagged": "lagged"},
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
    "rsi-divergence": {"rsi": "rsi"},
    "trend-strength-index": {"tsi": "tsi"},
    "williams-fractals": {"fractals": "fractals"},
    "williams-vix-fix": {
        "wvf": "wvf", "range_high": "rangeHigh",
        "range_low": "rangeLow", "upper_band": "upperBand",
    },
    "wavetrend": {"mom": "mom", "wt1": "wt1", "wt2": "wt2"},
}


class _OHLC:
    def __init__(self, o, h, l, c):
        self.open = _P(o); self.high = _P(h); self.low = _P(l); self.close = _P(c)


class _P:
    def __init__(self, v):
        self.value = Decimal(str(v))


class _Vol:
    def __init__(self, v):
        self.value = v


class _Candle:
    def __init__(self, time, o, h, l, c, v):
        IST = timezone(timedelta(hours=5, minutes=30))
        self.timestamp = datetime.fromtimestamp(time, tz=IST).replace(tzinfo=None)
        self.ohlc = _OHLC(o, h, l, c)
        self.volume = _Vol(v)


def _candles():
    bars = json.loads((GOLDENS / "fixtures.json").read_text())["bars"]
    return [_Candle(*b) for b in bars]


CANDLES = _candles()


def _golden_ids():
    return sorted(p.stem for p in GOLDENS.glob("*.json") if p.stem != "fixtures")


def _compare(name, actual, expected):
    assert len(actual) == len(expected), f"{name}: length {len(actual)} != {len(expected)}"
    for i, (a, e) in enumerate(zip(actual, expected)):
        if e is None:
            assert a is None, f"{name}[{i}]: expected None, got {a}"
        else:
            assert a is not None, f"{name}[{i}]: expected {e}, got None"
            assert a == pytest.approx(e, abs=1e-9), f"{name}[{i}]"


@pytest.mark.parametrize("ts_id", [gid for gid in _golden_ids()])
def test_parity_against_ts_source(ts_id):
    spec = get_indicator_spec(ts_id)
    if spec is None:
        pytest.skip(f"{ts_id}: not yet ported")
    if ts_id not in PARAM_MAP:
        pytest.skip(f"{ts_id}: param mapping not yet defined (later batch)")
    golden = json.loads((GOLDENS / f"{ts_id}.json").read_text())
    params = {py: golden["settings"][ts] for ts, py in PARAM_MAP[ts_id].items()}
    got = compute_indicator(ts_id, CANDLES, params)

    if ts_id == "supertrend":
        # TS splits the line into up/down by direction; backend emits one line
        # plus native up/down companions.
        up, down, direction = golden["plots"]["up"], golden["plots"]["down"], got["direction"]
        expected = [u if d == 1 else w for u, w, d in zip(up, down, direction)]
        _compare("line", got["line"], expected)
        _compare("up", got["up"], golden["plots"]["up"])
        _compare("down", got["down"], golden["plots"]["down"])
        return

    for bk, gk in PLOT_MAP.get(ts_id, {k: k for k in got}).items():
        _compare(f"{ts_id}.{bk}", got[bk], golden["plots"][gk])


def test_every_backend_indicator_has_param_mapping():
    """Registry ids beyond Batch 0 must be explicitly mapped or skipped."""
    for entry in __import__("tradex_analytics.indicators", fromlist=["indicator_catalogue"]).indicator_catalogue():
        assert entry["id"] in PARAM_MAP, f"{entry['id']} missing from PARAM_MAP"
