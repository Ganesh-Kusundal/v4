"""Frontend↔backend contract parity regressions.

These tests reproduce the exact payloads ``frontend/src/backend-indicators.ts``
puts on the wire today (defaults read through numeric inputs become
``Number(...)`` → JSON ``null`` for every string/select param) and the exact
candle shapes the compute route builds (tz-aware UTC timestamps via
``chart._candles_for_bars``). The golden suite cannot catch these: its candles
carry naive datetimes and it always passes clean params.
"""
from __future__ import annotations

import json
from datetime import datetime, timezone
from pathlib import Path

import pytest

from tradex_domain.instruments import Equity
from tradex_domain.enums import Timeframe
from tradex_brokers.common.market_builders import make_candle
from tradex_trading.analytics.indicators import (
    compute_indicator,
    get_indicator_spec,
    indicator_catalogue,
)


BARS = None  # replaced by deterministic in-module generator below


def _make_bars(n: int = 225) -> list[dict]:
    """Deterministic synthetic IST-session bars — mirrors the differential
    harness dataset (3 sessions, zero-volume and flat high==low bars)."""
    seed = 42

    def rnd() -> float:
        nonlocal seed
        seed = (seed * 1103515245 + 12345) % 2147483648
        return seed / 2147483648

    bars: list[dict] = []
    price = 1000.0
    for d in range(3):
        base = datetime(2024, 1, 1 + d, 3, 45, tzinfo=timezone.utc).timestamp()
        for m in range(75):
            if len(bars) >= n:
                break
            o = price
            c = max(500.0, o + (rnd() - 0.5) * 8)
            hi = max(o, c) + rnd() * 4
            lo = min(o, c) - rnd() * 4
            flat = m % 50 == 49
            vol = 0 if m % 37 == 0 else int(rnd() * 10000) + 100
            bars.append({
                "time": base + m * 60,
                "open": c if flat else o,
                "high": c if flat else hi,
                "low": c if flat else lo,
                "close": c,
                "volume": vol,
            })
            price = c
    return bars


BARS = _make_bars()


def api_shaped_candles():
    """Candles exactly as the compute route rebuilds them (aware UTC)."""
    instr = Equity.of("NSE", "RELIANCE")
    return [
        make_candle(
            instr,
            Timeframe.M1,
            open=b["open"], high=b["high"], low=b["low"], close=b["close"],
            volume=int(b["volume"] or 0),
            timestamp=datetime.fromtimestamp(b["time"], tz=timezone.utc),
        )
        for b in BARS
    ]


def frontend_payload(entry: dict) -> dict:
    """The params object backend-indicators.ts actually sends."""
    out = {}
    for p in entry["params"]:
        if p["type"] in ("int", "float"):
            out[p["name"]] = p["default"]
        else:
            # Number(<string default>) is NaN → JSON.stringify emits null.
            # Booleans survive as 1/0 numbers only when they were never edited;
            # the library keeps them verbatim, so mirror that.
            raw = p["default"]
            if isinstance(raw, bool):
                out[p["name"]] = 1 if raw else 0
            elif isinstance(raw, str):
                try:
                    out[p["name"]] = float(raw)
                except ValueError:
                    out[p["name"]] = None
            else:
                out[p["name"]] = raw
    return out


def test_cpr_survives_tz_aware_api_candles():
    """chart._candles_for_bars builds aware-UTC candles; CPR's session logic
    assumed naive-IST and raised TypeError on the live API path."""
    vals = compute_indicator("cpr", api_shaped_candles(), {})
    assert vals, "cpr produced no plots"
    # Daily pivot levels must materialise; coarser-period columns (weekly /
    # monthly) are legitimately null on a short synthetic window.
    assert any(
        any(v is not None for v in series)
        for key, series in vals.items()
        if key.startswith("d")
    ), "cpr daily pivot plots are all-null"


def test_all_specs_survive_frontend_null_payload():
    """Every registered indicator must compute with the exact param dict the
    browser sends (string/select params arrive as null)."""
    entries = {e["id"]: e for e in indicator_catalogue()}
    problems = []
    for iid, entry in sorted(entries.items()):
        payload = frontend_payload(entry)
        try:
            compute_indicator(iid, api_shaped_candles(), payload)
        except Exception as exc:  # noqa: BLE001 - this IS the assertion
            problems.append(f"{iid}: {type(exc).__name__}: {exc}")
    assert not problems, "indicators broken under frontend payloads:\n" + "\n".join(problems)


@pytest.mark.parametrize(
    "iid",
    ["ma-ribbon", "ppo", "volatility-stop", "relative-volatility-index"],
)
def test_null_params_equal_true_defaults(iid):
    """A null param means 'not supplied' — the result must equal a call made
    with no params at all, not a degraded/crashing computation."""
    defaults = compute_indicator(iid, api_shaped_candles(), {})
    nulls = {
        name: (None if isinstance(default, str) else default)
        for name, _kind, default in get_indicator_spec(iid).params
    }
    via_nulls = compute_indicator(iid, api_shaped_candles(), nulls)
    assert set(defaults) == set(via_nulls)
    for key in defaults:
        assert defaults[key] == via_nulls[key], f"{iid}.{key} diverges under null params"
