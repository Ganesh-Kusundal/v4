"""Alternate-settings parity: the focused indicator subset must match the
actual openalgo-charts 2.1.0 engine at two non-default setting variants
(1e-9, None-aligned). Goldens live under goldens/settings2/{variant}/{id}.json
as {id, settings, plots}, dumped by
openalgo-charts/scripts/dump-indicator-parity-multisettings.mjs."""
import json
from datetime import datetime, timedelta, timezone
from decimal import Decimal
from pathlib import Path

import pytest

from tradex_trading.analytics.indicators import compute_indicator

GOLDENS2 = Path(__file__).parent / "goldens" / "settings2"

VARIANTS = ("variantA", "variantB")

IDS = [
    "sma",
    "ema",
    "rsi",
    "macd",
    "bollinger",
    "atr",
    "supertrend",
    "vwap",
    "hma",
    "adx",
    "parabolic-sar",
    "hull-suite",
    "t3",
    "standard-error-bands",
    "chaikin-volatility",
]

# TS descriptor id -> {TS settings key: backend param name}. Legacy mappings
# copied exactly from test_golden_parity.py PARAM_MAP; the ids it does not
# cover are identity.
PARAM_MAP_21 = {
    "sma": {"length": "period", "source": "source"},
    "ema": {"length": "period", "source": "source"},
    "rsi": {"length": "period", "source": "source"},
    "macd": {"fastPeriod": "fast", "slowPeriod": "slow", "signalPeriod": "signal", "source": "source"},
    "bollinger": {"length": "period", "stdDev": "num_std", "source": "source"},
    # Legacy PARAM_MAP has "atr": {} and "supertrend": {}, which only works
    # when engine defaults coincide with backend defaults. At alternate
    # settings the inputs must be passed through (identity: TS key "period"
    # is already the backend key "period"), otherwise the backend would
    # silently compute defaults against non-default goldens.
    "atr": {"period": "period"},
    "supertrend": {"period": "period", "multiplier": "multiplier"},
    "vwap": {},
    "hma": {"length": "period", "source": "source"},
    "adx": {"period": "period", "adxPeriod": "adx_period"},
    "parabolic-sar": {"start": "start", "increment": "increment", "maximum": "maximum"},
    "hull-suite": {
        "source": "source", "mode": "mode", "length": "length",
        "lengthMult": "lengthMult", "visualSwitch": "visualSwitch",
    },
    "t3": {"length": "length", "factor": "factor", "source": "source"},
    "standard-error-bands": {
        "periods": "periods", "errors": "errors", "method": "method",
        "averagePeriods": "averagePeriods",
    },
    "chaikin-volatility": {"periods": "periods", "rocLookback": "rocLookback"},
}

# backend plot key -> TS golden plot key (identity when absent).
PLOT_MAP_21 = {
    "sma": {"value": "ma"},
    "ema": {"value": "ma"},
    "rsi": {"value": "rsi"},
    "bollinger": {"middle": "basis", "upper": "upper", "lower": "lower"},
    "atr": {"value": "atr"},
    "vwap": {"value": "vwap"},
    "hma": {"value": "hma"},
    "adx": {"plusDi": "plusDi", "minusDi": "minusDi", "adx": "adx"},
    "parabolic-sar": {"sar": "sar"},
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
    bars = json.loads((GOLDENS2.parent / "fixtures.json").read_text())["bars"]
    return [_Candle(*b) for b in bars]


CANDLES = _candles()


def _compare(name, actual, expected):
    assert len(actual) == len(expected), f"{name}: length {len(actual)} != {len(expected)}"
    for i, (a, e) in enumerate(zip(actual, expected)):
        if e is None:
            assert a is None, f"{name}[{i}]: expected None, got {a}"
        else:
            assert a is not None, f"{name}[{i}]: expected {e}, got None"
            assert a == pytest.approx(e, abs=1e-9), f"{name}[{i}]"


@pytest.mark.parametrize("variant", VARIANTS)
@pytest.mark.parametrize("ts_id", IDS)
def test_parity_at_alternate_settings(variant, ts_id):
    golden = json.loads((GOLDENS2 / variant / f"{ts_id}.json").read_text())
    assert golden["id"] == ts_id
    params = {py: golden["settings"][ts] for ts, py in PARAM_MAP_21[ts_id].items()}
    got = compute_indicator(ts_id, CANDLES, params)

    if ts_id == "supertrend":
        # TS splits the line into up/down by direction; backend emits one line.
        up, down, direction = golden["plots"]["up"], golden["plots"]["down"], got["direction"]
        expected = [u if d == 1 else w for u, w, d in zip(up, down, direction)]
        _compare(f"{variant}.{ts_id}.line", got["line"], expected)
        return

    for bk, gk in PLOT_MAP_21.get(ts_id, {k: k for k in got}).items():
        _compare(f"{variant}.{ts_id}.{bk}", got[bk], golden["plots"][gk])
