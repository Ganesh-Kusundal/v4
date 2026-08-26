"""Golden parity: every registered indicator must match the actual
openalgo-charts TypeScript implementation exactly (1e-9, None-aligned)."""
import json
from datetime import datetime, timedelta, timezone
from decimal import Decimal
from pathlib import Path

import pytest

from tradex_trading.analytics.indicators import compute_indicator, get_indicator_spec

GOLDENS = Path(__file__).parent / "goldens"

# TS descriptor id -> {TS settings key: backend param name}
PARAM_MAP = {
    "sma": {"length": "period"},
    "ema": {"length": "period"},
    "rsi": {"length": "period"},
    "roc": {"length": "period"},
    "macd": {"fastPeriod": "fast", "slowPeriod": "slow", "signalPeriod": "signal"},
    "bollinger": {"length": "period", "stdDev": "num_std"},
    "atr": {},
    "obv": {},
    "vwap": {},
    "stochastic": {"kPeriod": "k_period", "kSmoothing": "smooth_k", "dPeriod": "d_period"},
    "supertrend": {},
    "wma": {"length": "period"},
    "hma": {"length": "period"},
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
    "cci": {"period": "period", "constant": "constant"},
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
    "obv": {"value": "obv"},
    "vwap": {"value": "vwap"},
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
    "cci": {"cci": "cci"},
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
        # TS splits the line into up/down by direction; backend emits one line.
        up, down, direction = golden["plots"]["up"], golden["plots"]["down"], got["direction"]
        expected = [u if d == 1 else w for u, w, d in zip(up, down, direction)]
        _compare("line", got["line"], expected)
        return

    for bk, gk in PLOT_MAP.get(ts_id, {k: k for k in got}).items():
        _compare(f"{ts_id}.{bk}", got[bk], golden["plots"][gk])


def test_every_backend_indicator_has_param_mapping():
    """Registry ids beyond Batch 0 must be explicitly mapped or skipped."""
    for entry in __import__("tradex_trading.analytics.indicators", fromlist=["indicator_catalogue"]).indicator_catalogue():
        assert entry["id"] in PARAM_MAP, f"{entry['id']} missing from PARAM_MAP"
