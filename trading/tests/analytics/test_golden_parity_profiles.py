"""Golden parity: every profile study must match the actual openalgo-charts
TypeScript implementation exactly (1e-9, None-aligned)."""
import json
from pathlib import Path

import pytest

from tradex_trading.analytics.profiles import compute_profile
from tradex_trading.analytics.seasonality import compute_seasonality

GOLDENS = Path(__file__).parent / "goldens" / "profiles"

FIXTURES = json.loads(
    (Path(__file__).parent / "goldens" / "fixtures.json").read_text()
)
BARS = [
    {"time": t, "open": o, "high": h, "low": low, "close": c, "volume": v}
    for t, o, h, low, c, v in FIXTURES["bars"]
]

# profile id -> {TS setting key: backend param name}
PARAM_MAP = {
    "volume-profile": {
        "tickSize": "tick_size",
        "valueAreaPercent": "value_area_percent",
    },
    "tpo": {
        "periodBars": "period_bars",
        "tickSize": "tick_size",
        "valueAreaPercent": "value_area_percent",
        "ibPeriods": "ib_periods",
    },
    "market-profile": {
        "tickSize": "tick_size",
        "rowTicks": "row_ticks",
        "session": "session",
        "blockMinutes": "block_minutes",
        "valueAreaPercent": "value_area_percent",
        "initialBalancePeriods": "initial_balance_periods",
        "compositeSessions": "composite_sessions",
        "tailEdges": "tail_edges",
    },
    "footprint": {"time": "time", "tickSize": "tick_size", "rowTicks": "row_ticks"},
}


def _round_floats(obj):
    if isinstance(obj, float):
        return round(obj, 9)
    if isinstance(obj, list):
        return [_round_floats(v) for v in obj]
    if isinstance(obj, dict):
        return {k: _round_floats(v) for k, v in obj.items()}
    return obj


def _build_footprint_trades(bars):
    # Deterministic synthetic trades mirroring the generator's rule: per bar,
    # one ask print at high for volume/2, one bid print at low for volume/2.
    trades = []
    for b in bars:
        half = (b["volume"] or 0) / 2
        trades.append({"price": b["high"], "qty": half, "side": "ask"})
        trades.append({"price": b["low"], "qty": half, "side": "bid"})
    return trades


def _seasonality_bars():
    # Deterministic 13-month series mirroring the generator (Jan 2025..Jan 2026).
    from datetime import UTC, datetime
    bars = []
    for m in range(13):
        year, month = (2025 + m // 12, m % 12 + 1)
        ts = int(datetime(year, month, 1, tzinfo=UTC).timestamp())
        close = 100.0 + 3 * m
        bars.append(
            {
                "time": ts,
                "open": close - 1,
                "high": close + 1,
                "low": close - 2,
                "close": close,
                "volume": 1000,
            }
        )
    return bars


@pytest.mark.parametrize("pid", ["volume-profile", "tpo", "market-profile", "footprint"])
def test_profile_matches_ts_golden(pid: str) -> None:
    golden = json.loads((GOLDENS / f"{pid}.json").read_text())
    params = {py: golden["settings"][ts] for ts, py in PARAM_MAP[pid].items()}
    bars = _build_footprint_trades(BARS) if pid == "footprint" else BARS
    got = compute_profile(pid, bars, params)
    assert _round_floats(got) == _round_floats(golden["result"]), f"{pid} mismatch"


def test_seasonality_matches_ts_golden() -> None:
    golden = json.loads((GOLDENS / "seasonality.json").read_text())
    got = compute_seasonality(_seasonality_bars(), {"start_year": 2015, "ignored_months": ""})
    assert _round_floats(got) == _round_floats(golden["result"])


def test_unknown_profile_id_fails_loudly() -> None:
    with pytest.raises(ValueError):
        compute_profile("no-such-profile", BARS, {})
