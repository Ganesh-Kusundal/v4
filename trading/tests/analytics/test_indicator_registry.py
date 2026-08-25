"""Indicator registry tests — golden values, param validation, catalogue."""

from __future__ import annotations

from datetime import datetime
from decimal import Decimal

import pytest

from tradex_trading.analytics.indicators import (
    IndicatorSpec,
    atr,
    bollinger,
    compute_indicator,
    indicator_catalogue,
    obv,
    register_indicator,
    rsi,
    sma,
    stochastic,
    supertrend,
    true_ranges,
    vwap_session,
)


# ---------------------------------------------------------------------------
# Candle fixture — tiny domain-shaped objects (only what indicators read)
# ---------------------------------------------------------------------------


class _OHLC:
    def __init__(self, o: float, h: float, l: float, c: float) -> None:
        self.open = _P(o)
        self.high = _P(h)
        self.low = _P(l)
        self.close = _P(c)


class _P:
    def __init__(self, v: float) -> None:
        self.value = Decimal(str(v))


class _Vol:
    def __init__(self, v: float) -> None:
        self.value = v


def _candle(
    o: float,
    h: float,
    l: float,
    c: float,
    v: float = 100.0,
    ts: datetime | None = None,
):
    """A duck-typed candle: only .ohlc.{o,h,l,c}, .volume, .timestamp are read."""

    class C:
        pass

    inst = C()
    inst.ohlc = _OHLC(o, h, l, c)
    inst.volume = _Vol(v)
    inst.timestamp = ts or datetime(2026, 7, 15, 9, 15)
    return inst


CLOSES = [10.0, 11.0, 12.0, 11.5, 12.5, 13.0, 12.0, 12.8, 13.4, 14.0]


def _candles_from_closes(closes: list[float]) -> list:
    out = []
    minute = 0
    for c in closes:
        out.append(_candle(c - 0.5, c + 0.5, c - 1.0, c, 100.0,
                           datetime(2026, 7, 15, 9, 15) + __import__("datetime").timedelta(minutes=minute)))
        minute += 1
    return out


# ---------------------------------------------------------------------------
# Golden values
# ---------------------------------------------------------------------------


class TestGoldenValues:
    def test_sma_period3(self):
        result = sma(CLOSES, 3)
        assert result[2] == pytest.approx((10 + 11 + 12) / 3)
        assert result[3] == pytest.approx((11 + 12 + 11.5) / 3)
        assert result[:2] == [None, None]

    def test_rsi_all_gains_is_100(self):
        rising = [float(i) for i in range(1, 20)]
        assert rsi(rising, 14)[-1] == 100.0

    def test_atr_constant_range(self):
        candles = [_candle(10, 12, 9, 11)] * 20
        values = atr(candles, period=14)
        assert values[-1] == pytest.approx(3.0)
        # TS parity: seed lands at index period-1 (Wilder SMA over
        # TR[0..13]); everything before is warmup.
        assert values[13] == pytest.approx(3.0)
        assert all(v is None for v in values[:13])
        # TR[0] = high - low (=3), so the seed includes it as a full member.
        true_ranges_out = true_ranges(candles)
        assert true_ranges_out[0] == pytest.approx(3.0)

    def test_bollinger_flat_series(self):
        flat = [50.0] * 25
        bands = bollinger(flat, period=20, num_std=2.0)
        assert bands["middle"][-1] == 50.0
        assert bands["upper"][-1] == pytest.approx(50.0)
        assert bands["lower"][-1] == pytest.approx(50.0)

    def test_bollinger_widens_with_dispersion(self):
        spread = [50.0 + (i % 2) * 4.0 for i in range(25)]
        bands = bollinger(spread, period=20, num_std=2.0)
        assert bands["upper"][-1] > bands["middle"][-1] > bands["lower"][-1]
        # Alternating 0/4 offsets: population sd is 2, so the band sits at
        # exactly num_std * sd above/below the middle.
        assert bands["upper"][-1] - bands["middle"][-1] == pytest.approx(4.0)

    def test_obv_direction_signing(self):
        candles = [
            _candle(9, 11, 8, 10, 100),
            _candle(10, 13, 9, 12, 200),  # up close -> +200
            _candle(12, 13, 10, 11, 300),  # down close -> -300
            _candle(11, 12, 10, 11, 400),  # flat close -> no change
        ]
        result = obv(candles)
        assert result == [0.0, 200.0, -100.0, -100.0]

    def test_stochastic_bounds_and_alignment(self):
        candles = _candles_from_closes(CLOSES)
        result = stochastic(candles, k_period=5, d_period=3)
        n = len(CLOSES)
        assert len(result["k"]) == n and len(result["d"]) == n
        for v in result["k"]:
            if v is not None:
                assert 0.0 <= v <= 100.0
        # %D starts one d_period later than %K
        assert result["k"][4] is not None and result["d"][4] is None
        assert result["d"][6] is not None

    def test_supertrend_flip_on_breakdown(self):
        closes = [50.0 + i for i in range(15)] + [
            64.0 - 4.0 * i for i in range(1, 6)
        ]
        candles = _candles_from_closes(closes)
        result = supertrend(candles, period=5, multiplier=1.0)
        dirs = [d for d in result["direction"] if d is not None]
        assert set(dirs) <= {1, -1}
        assert -1 in dirs  # the breakdown must register a flip

    def test_vwap_resets_each_session(self):
        from datetime import timedelta

        base = datetime(2026, 7, 15, 9, 15)
        day1 = [_candle(10, 12, 9, 11, 100, base + timedelta(minutes=i)) for i in range(5)]
        day2 = [
            _candle(20, 22, 19, 21, 100, base + timedelta(days=1, minutes=i))
            for i in range(5)
        ]
        result = vwap_session(day1 + day2)
        # Day 2's first VWAP must be day-2-only (reset), not blended with day 1.
        assert result[5] == pytest.approx((21 + 22 + 19) / 3)
        assert result[0] == pytest.approx((12 + 9 + 11) / 3)

    def test_vwap_volume_weighting(self):
        from datetime import timedelta

        base = datetime(2026, 7, 15, 9, 15)
        c1 = _candle(10, 12, 9, 11, 900, base)
        c2 = _candle(20, 24, 18, 22, 100, base + timedelta(minutes=1))
        result = vwap_session([c1, c2])
        tp1 = (12 + 9 + 11) / 3
        tp2 = (24 + 18 + 22) / 3
        expected = (tp1 * 900 + tp2 * 100) / 1000
        assert result[1] == pytest.approx(expected)


# ---------------------------------------------------------------------------
# Registry contract
# ---------------------------------------------------------------------------


class TestRegistry:
    def test_catalogue_has_core_set(self):
        ids = {entry["id"] for entry in indicator_catalogue()}
        assert {
            "sma", "ema", "rsi", "roc", "macd", "bollinger", "atr",
            "vwap", "obv", "stochastic", "supertrend",
        } <= ids

    def test_catalogue_entries_shape(self):
        for entry in indicator_catalogue():
            assert entry["placement"] in {"overlay", "pane"}
            # Params may be empty (VWAP, OBV take none) but must be well-formed.
            for p in entry["params"]:
                assert {"name", "type", "default"} <= set(p)
            assert entry["plots"], entry["id"]

    def test_compute_unknown_param_rejected(self):
        candles = _candles_from_closes(CLOSES)
        with pytest.raises(ValueError, match="unknown params"):
            compute_indicator("sma", candles, {"periodx": 5})

    def test_compute_unknown_id_rejected(self):
        with pytest.raises(ValueError, match="unknown indicator"):
            compute_indicator("nope", [], {})

    def test_compute_sma_matches_direct(self):
        candles = _candles_from_closes(CLOSES)
        via_registry = compute_indicator("sma", candles, {"period": 3})
        assert via_registry["value"][2] == pytest.approx((10 + 11 + 12) / 3)

    def test_registration_overrides_by_id(self):
        spec = IndicatorSpec(
            id="zzz-test-only", name="Z", category="T", placement="pane",
            params=(("period", "int", 2),), plots=(("value", "line", "V"),),
        )
        before = {e["id"] for e in indicator_catalogue()}
        register_indicator(spec)
        after = {e["id"] for e in indicator_catalogue()}
        assert "zzz-test-only" in after and "zzz-test-only" not in before
        # A second registration of the same id must replace, not duplicate.
        count_before = sum(1 for e in indicator_catalogue() if e["id"] == "zzz-test-only")
        register_indicator(spec)
        count_after = sum(1 for e in indicator_catalogue() if e["id"] == "zzz-test-only")
        assert count_before == count_after == 1

    def test_new_registry_entry_reaches_catalogue_with_zero_frontend_changes(self):
        """The seam proof: registering an id is enough for it to be served."""
        marker = "aa-seam-proof"
        register_indicator(
            IndicatorSpec(
                id=marker, name="Seam", category="Test", placement="pane",
                params=(), plots=(("value", "line", "S"),),
            )
        )
        ids = {e["id"] for e in indicator_catalogue()}
        assert marker in ids
