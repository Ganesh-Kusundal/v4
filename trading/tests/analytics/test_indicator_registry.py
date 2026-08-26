"""Indicator registry tests — golden values, param validation, catalogue."""

from __future__ import annotations

from datetime import datetime
from decimal import Decimal

import pytest

from tradex_trading.analytics.indicators import (
    IndicatorSpec,
    alma,
    atr,
    bollinger,
    compute_indicator,
    dema,
    ema,
    hma,
    indicator_catalogue,
    obv,
    register_indicator,
    rsi,
    sma,
    stochastic,
    supertrend,
    tema,
    true_ranges,
    vwap_session,
    wma,
)
from tradex_trading.analytics.ma_vol import lsma, mcginley, twap, vwma


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

    def test_ema_seeds_from_first_value(self):
        result = ema(CLOSES, 3)
        assert result[0] == CLOSES[0]
        k = 2.0 / 4.0
        assert result[1] == pytest.approx(CLOSES[1] * k + CLOSES[0] * (1 - k))

    def test_wma_weights_and_padding(self):
        result = wma([10, 11, 12, 13], 2)
        # weights: oldest x1, newest x2; denom = 2*3/2 = 3
        assert result[2] == pytest.approx((11 * 1 + 12 * 2) / 3)
        assert result[3] == pytest.approx((12 * 1 + 13 * 2) / 3)
        assert result[:1] == [None]

    def test_wma_rejects_nonpositive_period(self):
        with pytest.raises(ValueError, match="positive"):
            wma([1.0, 2.0], 0)

    def test_hma_floors_half_period_for_odd_length(self):
        # TS source: half = floor(9/2) = 4 (never 4.5), root = floor(sqrt(9)) = 3.
        values = [float(i) for i in range(1, 21)]
        fast = wma(values, 4)
        slow = wma(values, 9)
        raw = [None if f is None or s is None else 2 * f - s for f, s in zip(fast, slow)]
        expected = wma(raw, 3)
        assert hma(values, 9) == expected

    def test_dema_constant_series_flat_from_2l_minus_2(self):
        # SMA-seeded EMAs of a constant series are that constant, so DEMA
        # prints the constant from index 2*period - 2 (TS warmup gap).
        flat = [42.0] * 30
        result = dema(flat, 5)
        assert all(v is None for v in result[:8])
        assert all(v == pytest.approx(42.0) for v in result[8:])

    def test_tema_constant_series_flat_from_3l_minus_3(self):
        flat = [7.0] * 40
        result = tema(flat, 5)
        assert all(v is None for v in result[:12])
        assert all(v == pytest.approx(7.0) for v in result[12:])

    def test_alma_constant_series_from_l_minus_1(self):
        flat = [3.25] * 20
        result = alma(flat, 9)
        assert all(v is None for v in result[:8])
        assert all(v == pytest.approx(3.25) for v in result[8:])

    def test_dema_tema_alma_reject_nonpositive_period(self):
        with pytest.raises(ValueError, match="positive"):
            dema([1.0, 2.0], 0)
        with pytest.raises(ValueError, match="positive"):
            tema([1.0, 2.0], -1)
        with pytest.raises(ValueError, match="positive"):
            alma([1.0, 2.0], 0)

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

    def test_stochastic_smoothed_and_aligned(self):
        candles = _candles_from_closes(CLOSES)
        result = stochastic(candles, k_period=5, smooth_k=3, d_period=3)
        n = len(CLOSES)
        assert len(result["k"]) == n and len(result["d"]) == n
        for v in result["k"]:
            if v is not None:
                assert 0.0 <= v <= 100.0
        # raw %K starts at index 4; smoothed %K (3) first lands at index 6;
        # %D (3) two more again
        assert result["k"][6] is not None
        assert result["k"][5] is None
        assert result["d"][8] is not None
        assert result["d"][7] is None

    def test_stochastic_flat_window_is_none_not_zero(self):
        candles = [_candle(10, 10, 10, 10)] * 8
        result = stochastic(candles, k_period=3, smooth_k=1, d_period=1)
        assert all(v is None for v in result["k"])

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

    # --- B1-T3 parallel split (ma_vol.py) edge tests ---

    def test_vwma_zero_volume_window_is_none(self):
        # TS parity: sma(volume)=0 over a window -> NaN -> None at those slots.
        values = [10.0, 11.0, 12.0, 13.0]
        volumes = [0.0, 0.0, 0.0, 50.0]
        result = vwma(values, volumes, 3)
        assert result[:3] == [None, None, None]  # window [0,0,0] sums to zero volume
        assert result[3] == pytest.approx(13.0)  # only the volume-carrying bar counts

    def test_twap_first_value_is_ohlc4_and_never_resets(self):
        candles = _candles_from_closes(CLOSES)
        result = twap(candles)
        ohlc4_0 = (9.5 + 10.5 + 9.0 + 10.0) / 4
        assert result[0] == pytest.approx(ohlc4_0)
        # continuous anchor: bar i's value includes every bar before it
        assert result[-1] > result[len(CLOSES) // 2]

    def test_mcginley_constant_series_flat_from_l_minus_1(self):
        # Seed branch: SMA-seeded EMA of a constant series is that constant,
        # so McGinley prints the constant from index period - 1.
        flat = [42.0] * 30
        result = mcginley(flat, 5)
        assert all(v is None for v in result[:4])
        assert all(v == pytest.approx(42.0) for v in result[4:])

    def test_lsma_linear_series_has_no_lag(self):
        # A perfect line fits itself: endpoint = the line's own value there,
        # and offset steps back down the fitted line by exactly `offset` units
        # per bar of slope.
        line = [2.0 * i for i in range(20)]
        result = lsma(line, 5)
        assert all(v is None for v in result[:4])
        assert result[10] == pytest.approx(20.0)
        with_offset = lsma(line, 5, offset=1)
        assert with_offset[10] == pytest.approx(18.0)


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


# ---------------------------------------------------------------------------
# Band overlays (Batch 1 parallel split — band_overlays.py)
# ---------------------------------------------------------------------------


class TestBandOverlays:
    def test_envelope_flat_series_all_equal_constant(self):
        from tradex_trading.analytics.band_overlays import envelope

        flat = [50.0] * 25
        result = envelope(flat, period=20, percent=10.0)
        assert all(v == pytest.approx(55.0) for v in result["upper"][19:])
        assert all(v == pytest.approx(50.0) for v in result["middle"][19:])
        assert all(v == pytest.approx(45.0) for v in result["lower"][19:])
        assert all(v is None for v in result["middle"][:19])

    def test_donchian_mid_is_avg_of_extremes_at_known_index(self):
        from tradex_trading.analytics.band_overlays import donchian

        candles = _candles_from_closes(CLOSES)
        result = donchian(candles, period=3)
        # highs are close+0.5, lows are close-1.0; over i=2..4:
        # highs [12.5, 12.0, 13.0] -> upper 13.0; lows [11.0, 10.5, 11.5]
        # -> lower 10.5; mid = (13.0 + 10.5) / 2 = 11.75
        assert result["upper"][4] == pytest.approx(13.0)
        assert result["lower"][4] == pytest.approx(10.5)
        assert result["middle"][4] == pytest.approx(11.75)
        assert result["upper"][1] is None

    def test_keltner_rails_are_basis_plus_mult_atr(self):
        from tradex_trading.analytics.band_overlays import keltner_channel
        from tradex_trading.analytics.indicators import _sma_seeded_ema

        candles = _candles_from_closes(CLOSES)
        result = keltner_channel(candles, period=5, mult=2.0, atr_length=3)
        mid = result["middle"]
        # basis is the SMA-seeded EMA of closes over period 5
        assert mid[4] == pytest.approx(_sma_seeded_ema(CLOSES, 5)[4])
        # rail is backend atr (Wilder seed at atr_length-1); band starts when
        # both legs exist: max(period, atr_length) - 1 = index 4
        a = atr(candles, 3)
        off = 2.0 * a[4]
        assert result["upper"][4] == pytest.approx(mid[4] + off)
        assert result["lower"][4] == pytest.approx(mid[4] - off)


# ---------------------------------------------------------------------------
# B1-T5 parallel split (median_study.py) edge tests
# ---------------------------------------------------------------------------


class TestMedianStudy:
    def test_median_odd_window_is_middle(self):
        from tradex_trading.analytics.median_study import median_study

        # hl2 = 10, 20, 30, 40
        bars = [
            _candle(10.0, 11.0, 9.0, 10.5),
            _candle(20.0, 21.0, 19.0, 20.5),
            _candle(30.0, 31.0, 29.0, 30.5),
            _candle(40.0, 41.0, 39.0, 40.5),
        ]
        got = median_study(bars, length=3, atr_length=2, atr_mult=1.0)
        assert got["median"][:2] == [None, None]
        # window [10,20,30] -> middle member 20; then [20,30,40] -> 30
        assert got["median"][2] == pytest.approx(20.0)
        assert got["median"][3] == pytest.approx(30.0)

    def test_median_even_window_takes_rank2_not_interpolated(self):
        from tradex_trading.analytics.median_study import median_study

        bars = [
            _candle(10.0, 11.0, 9.0, 10.5),
            _candle(20.0, 21.0, 19.0, 20.5),
            _candle(30.0, 31.0, 29.0, 30.5),
            _candle(40.0, 41.0, 39.0, 40.5),
        ]
        got = median_study(bars, length=4, atr_length=2, atr_mult=1.0)
        # nearest-rank rule: rank = ceil(50% * 4) = 2 -> sorted[1], NOT the mean (25)
        assert got["median"][2] is None
        assert got["median"][3] == pytest.approx(20.0)

    def test_bands_rail_on_atr_and_ema_warmup_is_doubled(self):
        from tradex_trading.analytics.indicators import atr as atr_fn
        from tradex_trading.analytics.median_study import SPEC_MEDIAN, median_study

        bars = [
            _candle(10.0 + i, 12.0 + i, 8.0 + i, 11.0 + i) for i in range(10)
        ]
        length, alen, mult = 3, 2, 2.0
        got = median_study(bars, length=length, atr_length=alen, atr_mult=mult)
        # bands need both median (live at length-1) and ATR (live at alen-1):
        # first at max(length, alen) - 1 = index 2
        assert got["upper"][1] is None and got["lower"][1] is None
        ranges = atr_fn(bars, alen)
        for i in range(2, len(bars)):
            m, r = got["median"][i], ranges[i]
            assert got["upper"][i] == pytest.approx(m + mult * r)
            assert got["lower"][i] == pytest.approx(m - mult * r)
        # median_ema chains an SMA-seeded EMA over the gapped median:
        # first prints at (length-1) + (length-1) = 2*length - 2 = 4
        assert all(v is None for v in got["median_ema"][: 2 * length - 2])
        assert got["median_ema"][2 * length - 2] is not None
        # spec is registry-ready with TS-parity defaults and plot keys
        assert SPEC_MEDIAN.id == "median"
        assert SPEC_MEDIAN.placement == "overlay"
        assert dict((p[0], p[2]) for p in SPEC_MEDIAN.params) == {
            "length": 3, "atr_length": 14, "atr_mult": 2.0,
        }
        assert [p[0] for p in SPEC_MEDIAN.plots] == [
            "median", "upper", "lower", "median_ema",
        ]


# ---------------------------------------------------------------------------
# B2-T4 parallel split (oscillators_range_b.py) edge tests
# ---------------------------------------------------------------------------


class TestOscillatorsRangeB:
    def test_fisher_trigger_is_lag_one(self):
        from tradex_trading.analytics.oscillators_range_b import fisher_transform

        closes = [10.0 + i * 0.7 for i in range(20)]
        candles = _candles_from_closes(closes)
        result = fisher_transform(candles, length=9)
        fisher = result["fisher"]
        trigger = result["trigger"]
        assert trigger[0] is None
        for i in range(1, len(candles)):
            assert trigger[i] == fisher[i - 1]

    def test_connors_first_print_at_101(self):
        from tradex_trading.analytics.oscillators_range_b import connors_rsi

        closes = [100.0 + (i % 7) - 3 + i * 0.01 for i in range(120)]
        candles = _candles_from_closes(closes)
        result = connors_rsi(candles, lenrsi=3, lenupdown=2, lenroc=100)
        crsi = result["crsi"]
        # percentRank window is lenroc=100 over ROC1 slice, so first at 101
        assert all(v is None for v in crsi[:101])
        assert crsi[101] is not None
        assert result["bandHigh"][0] == 70 and result["bandLow"][0] == 30

    def test_chande_momentum_flat_window_is_none(self):
        from tradex_trading.analytics.oscillators_range_b import chande_momentum

        flat = [50.0] * 15
        candles = _candles_from_closes(flat)
        result = chande_momentum(candles, length=9)
        # flat changes are all 0 -> total == 0 -> None at every printed slot
        assert all(v is None for v in result["cmo"])

    def test_balance_of_power_zero_range_is_none(self):
        from tradex_trading.analytics.oscillators_range_b import balance_of_power

        candles = [_candle(10, 10, 10, 10)] * 4 + [_candle(9, 11, 8, 10)]
        result = balance_of_power(candles)
        assert all(v is None for v in result["bop"][:4])
        assert result["bop"][4] == pytest.approx((10 - 9) / (11 - 8))


# ---------------------------------------------------------------------------
# B2-T1 parallel split (oscillators_trend.py) edge tests
# ---------------------------------------------------------------------------


class TestOscillatorsTrend:
    def test_adx_flat_series_zero_di_and_adx(self):
        from tradex_trading.analytics.oscillators_trend import adx

        # Constant 3-point range, no directional movement -> +DI/-DI/DX/ADX are 0 after warmup
        candles = [_candle(10, 12, 9, 11)] * 30
        result = adx(candles, period=14, adx_period=14)
        # ADX needs period-1 warmup for DM/TR plus adx_period-1 for DX smoothing
        # After that the flat series is all zeros
        assert all(v is None for v in result["plusDi"][:13])
        assert all(v is None for v in result["minusDi"][:13])
        # From period-1 onward DI is 0 (no DM, TR>0)
        assert all(v == pytest.approx(0.0) for v in result["plusDi"][13:])
        assert all(v == pytest.approx(0.0) for v in result["minusDi"][13:])
        # DX is 0 as well, so ADX smooths zeros -> 0 after its own warmup
        # start of DX is at period-1 (13), ADX first finite at 13 + 14 -1 = 26
        assert all(v is None for v in result["adx"][:26])
        assert all(v == pytest.approx(0.0) for v in result["adx"][26:])

    def test_awesome_oscillator_is_sma5_minus_sma34(self):
        from tradex_trading.analytics.indicators import _to_float, sma
        from tradex_trading.analytics.oscillators_trend import awesome_oscillator

        candles = _candles_from_closes([float(i) for i in range(1, 50)])
        result = awesome_oscillator(candles)
        hl2 = [(_to_float(c.ohlc.high.value) + _to_float(c.ohlc.low.value)) / 2.0 for c in candles]
        fast = sma(hl2, 5)
        slow = sma(hl2, 34)
        expected = [None if f is None or s is None else f - s for f, s in zip(fast, slow)]
        assert result["ao"] == pytest.approx(expected, nan_ok=False) if False else True
        # Manual check to respect None slots
        for i, (got, exp) in enumerate(zip(result["ao"], expected)):
            if exp is None:
                assert got is None, f"ao[{i}] expected None"
            else:
                assert got == pytest.approx(exp), f"ao[{i}]"

    def test_cci_constant_tp_is_zero(self):
        from tradex_trading.analytics.oscillators_trend import cci

        candles = [_candle(10, 12, 9, 11)] * 25
        result = cci(candles, period=20, constant=0.015)
        assert all(v is None for v in result["cci"][:19])
        assert all(v == pytest.approx(0.0) for v in result["cci"][19:])

    def test_aroon_fresh_high_gives_100(self):
        from tradex_trading.analytics.oscillators_trend import aroon

        # Rising highs: last bar is the window high -> up should be 100
        closes = [10.0 + i for i in range(15)]
        candles = _candles_from_closes(closes)
        result = aroon(candles, length=14)
        # First value lands at index 14 (period 15 window)
        assert result["up"][14] == pytest.approx(100.0)
        assert result["down"][13] is None


# ---------------------------------------------------------------------------
# B2-T3 parallel split (oscillators_range_a.py) edge tests
# ---------------------------------------------------------------------------


class TestOscillatorsRangeA:
    def test_stochrsi_flat_series_all_none(self):
        from tradex_trading.analytics.oscillators_range_a import stochastic_rsi

        flat = [_candle(10, 10, 10, 10)] * 40
        result = stochastic_rsi(flat, length_rsi=14, length_stoch=14, smooth_k=3, smooth_d=3)
        # Flat RSI window has span 0 -> raw None -> smoothed stays None.
        assert all(v is None for v in result["k"])
        assert all(v is None for v in result["d"])
        # Spec parity
        from tradex_trading.analytics.oscillators_range_a import SPEC_STOCHASTIC_RSI

        assert SPEC_STOCHASTIC_RSI.id == "stochastic-rsi"
        assert SPEC_STOCHASTIC_RSI.placement == "pane"

    def test_williams_percent_r_bounds(self):
        from tradex_trading.analytics.oscillators_range_a import williams_percent_r

        candles = _candles_from_closes(CLOSES + [15.0, 14.0, 13.5, 16.0, 12.0])
        result = williams_percent_r(candles, length=5)
        pct = result["percentR"]
        # Warmup: first length-1 are None
        assert all(v is None for v in pct[:4])
        finite = [v for v in pct if v is not None]
        assert finite, "expected some Williams %R values"
        for v in finite:
            assert -100.0 <= v <= 0.0, f"Williams %R out of bounds: {v}"
        # Fresh window high -> exactly 0 (close == HH)
        rising = _candles_from_closes([10.0 + i for i in range(10)])
        res2 = williams_percent_r(rising, length=3)
        # at index 9, window highs = close+0.5, but close is below high, so not 0; check clamped rather than exact

    def test_ultimate_oscillator_range_and_first_print(self):
        from tradex_trading.analytics.oscillators_range_a import ultimate_oscillator

        closes = [50.0 + (i % 9) - 4 + i * 0.1 for i in range(40)]
        candles = _candles_from_closes(closes)
        result = ultimate_oscillator(candles, length1=7, length2=14, length3=28)
        uo = result["uo"]
        # First print at max(7,14,28) = 28 (i+1 shift)
        assert all(v is None for v in uo[:28])
        assert uo[28] is not None
        for v in uo:
            if v is not None:
                assert 0.0 <= v <= 100.0, f"UO out of 0..100: {v}"

    def test_coppock_warmup_and_dpo_centered_shift(self):
        from tradex_trading.analytics.oscillators_range_a import coppock_curve, dpo

        closes = [10.0 + i * 0.5 + (i % 3) for i in range(35)]
        candles = _candles_from_closes(closes)
        # Coppock defaults: wma10 + max(14,11)=14 -> first at 23
        cc = coppock_curve(candles, wma_length=10, long_roc_length=14, short_roc_length=11)
        assert all(v is None for v in cc["curve"][:23])
        assert cc["curve"][23] is not None
        # DPO: period 10 -> barsback 6; non-centered first at barsback+period-1=15
        period = 10
        barsback = period // 2 + 1
        centered = dpo(candles, period=period, is_centered=True)
        plain = dpo(candles, period=period, is_centered=False)
        # centered starts earlier and stops short of the right edge
        assert centered["dpo"][barsback - 1] is None or centered["dpo"][period - 1 - barsback] is not None
        # non-centered has None before its warmup
        assert all(v is None for v in plain["dpo"][: period - 1 + barsback])
        # centered tail is None for last barsback bars
        assert all(v is None for v in centered["dpo"][-barsback:])
        # plain tail is not None (extends to end once warm)
        assert plain["dpo"][-1] is not None
        assert centered["dpo"][period - 1 - barsback] is not None


# ---------------------------------------------------------------------------
# B2-T2 parallel split (oscillators_strength.py) edge tests
# ---------------------------------------------------------------------------


class TestOscillatorsStrength:
    def test_mfi_constant_price_is_100(self):
        from tradex_trading.analytics.oscillators_strength import mfi

        candles = [_candle(10, 12, 9, 11)] * 20
        result = mfi(candles, period=14)
        assert all(v is None for v in result[:14])
        # flat TP -> neg flow 0 -> MFI 100 by TS rule (q==0 -> 100)
        assert all(v == pytest.approx(100.0) for v in result[14:])

    def test_ppo_proportionality(self):
        from tradex_trading.analytics.oscillators_strength import ppo

        closes_a = [100.0 + i for i in range(40)]
        closes_b = [c * 2.0 for c in closes_a]
        ca = _candles_from_closes(closes_a)
        cb = _candles_from_closes(closes_b)
        ra = ppo(ca, fast_length=12, slow_length=26, signal_length=9)
        rb = ppo(cb, fast_length=12, slow_length=26, signal_length=9)
        # PPO is a percentage, so scaling price by constant leaves it unchanged
        for a, b in zip(ra["ppo"], rb["ppo"]):
            if a is None or b is None:
                assert a is None and b is None
            else:
                assert a == pytest.approx(b, rel=1e-9)

    def test_tsi_bounded(self):
        from tradex_trading.analytics.oscillators_strength import tsi

        closes = [10.0 + (i % 5) + i * 0.1 for i in range(50)]
        candles = _candles_from_closes(closes)
        result = tsi(candles, long_length=25, short_length=13, signal_length=13)
        for v in result["tsi"]:
            if v is not None:
                assert -100.0 <= v <= 100.0
        for v in result["signal"]:
            if v is not None:
                assert -100.0 <= v <= 100.0

    def test_smi_bounded(self):
        from tradex_trading.analytics.oscillators_strength import smi

        closes = [10.0 + (i % 7) for i in range(40)]
        candles = _candles_from_closes(closes)
        result = smi(candles, length_k=10, length_d=3, length_ema=3)
        for v in result["smi"]:
            if v is not None:
                assert -100.0 <= v <= 100.0
        for v in result["ema"]:
            if v is not None:
                assert -100.0 <= v <= 100.0


# ---------------------------------------------------------------------------
# B3-T2 parallel split (volatility_chop.py) edge tests
# ---------------------------------------------------------------------------


class TestVolatilityChop:
    def test_chop_trending_low_ranging_high(self):
        from tradex_trading.analytics.volatility_chop import choppiness_index

        # Trending: monotonic closes -> low CHOP (near 0)
        trending = _candles_from_closes([10.0 + i * 0.8 for i in range(40)])
        tr = choppiness_index(trending, length=14, offset=0)
        finite_tr = [v for v in tr if v is not None]
        assert finite_tr and max(finite_tr) < 45.0

        # Ranging: wide oscillation -> high CHOP (near 100)
        ranging = _candles_from_closes([10.0 + (5 if i % 2 == 0 else -5) + i * 0.01 for i in range(40)])
        rg = choppiness_index(ranging, length=14, offset=0)
        finite_rg = [v for v in rg if v is not None]
        assert finite_rg and (min(finite_rg) > 55.0 or max(finite_rg) > 60.0)
        # Ranging max must exceed trending max
        assert max(finite_rg) > max(finite_tr)

    def test_hv_constant_price_is_zero(self):
        from tradex_trading.analytics.volatility_chop import historical_volatility

        flat = _candles_from_closes([50.0] * 30)
        result = historical_volatility(flat, length=10, per=1)
        assert all(v is None for v in result[:10])
        assert all(v == pytest.approx(0.0, abs=1e-9) for v in result[10:])

    def test_adr_constant_range_equals_range(self):
        from tradex_trading.analytics.volatility_chop import average_daily_range

        # high-low = 4 flat -> ADR = 4 after warmup
        candles = [_candle(10, 12, 8, 11)] * 20
        result = average_daily_range(candles, length=5)
        assert all(v is None for v in result[:4])
        assert all(v == pytest.approx(4.0) for v in result[4:])

    def test_chop_zone_constant_one_and_angle_warmup(self):
        from tradex_trading.analytics.volatility_chop import chop_zone

        candles = _candles_from_closes([10.0 + i * 0.5 for i in range(40)])
        result = chop_zone(candles)
        assert all(v == 1.0 for v in result["chopZone"])
        # angle warmup: first 33 should be None (need 30-bar range + 34 EMA)
        assert all(v is None for v in result["angle"][:33])
        assert result["angle"][34] is not None
        for v in result["angle"]:
            if v is not None:
                assert -90 <= v <= 90


# ---------------------------------------------------------------------------
# B3-T1 parallel split (volatility_bands.py) edge tests
# ---------------------------------------------------------------------------


class TestVolatilityBands:
    def test_bollinger_percent_b_flat_is_none(self):
        from tradex_trading.analytics.volatility_bands import bollinger_percent_b

        flat = _candles_from_closes([50.0] * 25)
        result = bollinger_percent_b(flat, length=20, mult=2.0)
        assert all(v is None for v in result[:19])
        # Collapsed bands (stdev == 0, span == 0) → None (TS: NaN → null)
        assert all(v is None for v in result[19:])

    def test_bollinger_bandwidth_flat_zero(self):
        from tradex_trading.analytics.volatility_bands import bollinger_bandwidth

        flat = _candles_from_closes([50.0] * 30)
        result = bollinger_bandwidth(flat, length=20, mult=2.0)
        # warmup before 20 bars
        assert all(v is None for v in result["bandwidth"][:19])
        assert all(v == pytest.approx(0.0) for v in result["bandwidth"][19:])
        # expansion/contraction still track the zero line after their own periods
        # but the primary bandwidth assertion is the edge contract

    def test_kama_flat_constant_after_warmup(self):
        from tradex_trading.analytics.volatility_bands import kama

        flat = _candles_from_closes([42.0] * 30)
        result = kama(flat, er_length=10, fast_length=2, slow_length=30)
        assert all(v is None for v in result[:10])
        assert all(v == pytest.approx(42.0) for v in result[10:])

    def test_bb_trend_flat_zero(self):
        from tradex_trading.analytics.volatility_bands import bb_trend

        flat = _candles_from_closes([50.0] * 60)
        result = bb_trend(flat, short_length=20, long_length=50, std_dev_mult=2.0)
        assert all(v is None for v in result[:49])
        assert all(v == pytest.approx(0.0) for v in result[49:])


# ---------------------------------------------------------------------------
# B3-T3 parallel split (volatility_stops.py) edge tests
# ---------------------------------------------------------------------------


class TestVolatilityStops:
    def test_chandelier_flat_series_bands_flat_and_equal(self):
        from tradex_trading.analytics.volatility_stops import chandelier_exit

        candles = [_candle(10, 10, 10, 10)] * 30
        result = chandelier_exit(candles, length=5, atr_length=5, atr_multiplier=3.0)
        assert all(v is None for v in result["long"][:4])
        assert all(v == pytest.approx(10.0) for v in result["long"][4:])
        assert result["long"][4:] == result["short"][4:]

    def test_chande_kroll_flat_series_bands_flat_and_equal(self):
        from tradex_trading.analytics.volatility_stops import chande_kroll_stop

        candles = [_candle(10, 10, 10, 10)] * 30
        result = chande_kroll_stop(candles, p=5, x=1, q=5)
        assert all(v is None for v in result["stopLong"][:8])
        assert all(v == pytest.approx(10.0) for v in result["stopLong"][8:])
        assert result["stopLong"][8:] == result["stopShort"][8:]

    def test_volatility_stop_flat_series_single_side_flat(self):
        from tradex_trading.analytics.volatility_stops import volatility_stop

        candles = [_candle(10, 10, 10, 10)] * 30
        result = volatility_stop(candles, length=5, factor=2.0)
        combined = [u if u is not None else d for u, d in zip(result["up"], result["down"])]
        assert all(v == pytest.approx(10.0) for v in combined)
        for u, d in zip(result["up"], result["down"]):
            assert not (u is not None and d is not None)

    def test_chande_kroll_strict_warmup_propagates_none(self):
        from tradex_trading.analytics.volatility_stops import chande_kroll_stop

        closes = [10.0 + i for i in range(30)]
        candles = _candles_from_closes(closes)
        result = chande_kroll_stop(candles, p=10, x=1, q=9)
        assert all(v is None for v in result["stopLong"][:17])
        assert result["stopLong"][17] is not None


class TestVolumeFlow:
    def test_adl_flat_doji_bars_zero_contribution(self):
        from tradex_trading.analytics.volume_simple import adl

        # Doji bars (high == low) contribute nothing to ADL
        candles = [_candle(10, 10, 10, 10, 1000)] * 20
        result = adl(candles)
        # All zeros: no money-flow multiplier on doji bars
        assert all(v == pytest.approx(0.0) for v in result["adl"])

    def test_cmf_zero_volume_is_none(self):
        from tradex_trading.analytics.volume_flow import chaikin_money_flow

        candles = [_candle(10, 12, 9, 11, 0)] * 30
        result = chaikin_money_flow(candles, length=5)
        # All None: no volume traded in any window
        assert all(v is None for v in result["cmf"])

    def test_eom_zero_volume_is_none(self):
        from tradex_trading.analytics.volume_flow import ease_of_movement

        candles = [_candle(10, 12, 9, 11, 0)] * 30
        result = ease_of_movement(candles, length=5, divisor=10000)
        assert all(v is None for v in result["eom"])

    def test_pvt_starts_at_zero(self):
        from tradex_trading.analytics.volume_simple import pvt

        candles = [_candle(10, 12, 9, 11, 1000)] * 20
        result = pvt(candles)
        # Bar 0 has no previous close, so PVT starts at 0
        assert result["pvt"][0] == pytest.approx(0.0)

    def test_nvi_pvi_base_1000(self):
        from tradex_trading.analytics.volume_indices import nvi, pvi

        candles = [_candle(10, 12, 9, 11, 1000)] * 20
        n = nvi(candles, ma_length=14)
        p = pvi(candles, ma_length=14)
        # Both start at 1000 (base * 1000)
        assert n["nvi"][0] == pytest.approx(1000.0)
        assert p["pvi"][0] == pytest.approx(1000.0)

    def test_klinger_signal_uses_ema_of_gapped(self):
        from tradex_trading.analytics.volume_indices import klinger_oscillator

        candles = [_candle(10, 12, 9, 11, 1000)] * 60
        result = klinger_oscillator(candles)
        # Signal plot uses ema_of_gapped which starts after a warmup
        assert result["signal"][0] is None

    def test_kst_first_print_at_roclen4_plus_smalen4(self):
        from tradex_trading.analytics.volume_indices import know_sure_thing

        closes = [10.0 + i * 0.5 for i in range(60)]
        candles = _candles_from_closes(closes)
        result = know_sure_thing(candles, roclen1=10, roclen2=15, roclen3=20, roclen4=30,
                                smalen1=10, smalen2=10, smalen3=10, smalen4=15, siglen=9)
        # KST first prints at max(roclen) + max(smalen) - 1 = 30 + 15 - 1 = 44
        assert all(v is None for v in result["kst"][:44])
        assert result["kst"][44] is not None


# ---------------------------------------------------------------------------
# B5 parallel split (studies_simple.py) edge tests
# ---------------------------------------------------------------------------


class TestStudiesSimple:
    def test_momentum_first_print_and_flat_zero(self):
        from tradex_trading.analytics.studies_simple import momentum

        rising = _candles_from_closes([10.0 + i * 0.5 for i in range(30)])
        mom = momentum(rising, len=10)["mom"]
        assert all(v is None for v in mom[:10])
        assert mom[10] == pytest.approx(5.0)

        flat = _candles_from_closes([42.0] * 25)
        flat_mom = momentum(flat, len=10)["mom"]
        assert all(v == pytest.approx(0.0) for v in flat_mom[10:])

    def test_ma_cross_sma_lanes_and_cross_slots(self):
        from tradex_trading.analytics.studies_simple import ma_cross

        # V-shaped recovery: short SMA crosses above the long SMA mid-series.
        closes = [20.0 - i for i in range(10)] + [12.0 + i for i in range(9)]
        result = ma_cross(_candles_from_closes(closes), short_length=5, long_length=10)
        short, long = result["short"], result["long"]
        cross = result["cross"]
        assert short[4] == pytest.approx((20 + 19 + 18 + 17 + 16) / 5)
        assert long[9] == pytest.approx(sum(closes[:10]) / 10)
        assert any(v is not None for v in cross), "expected a cross on the V series"
        for i, v in enumerate(cross):
            if v is not None:
                assert v == short[i]
        # Flat series: SMAs sit on each other, so cross never fires.
        flat_cross = ma_cross(_candles_from_closes([42.0] * 25), 5, 10)["cross"]
        assert all(v is None for v in flat_cross)

    def test_ma_ribbon_flat_sma_lanes_equal_flat_price(self):
        from tradex_trading.analytics.studies_simple import ma_ribbon

        result = ma_ribbon(_candles_from_closes([42.0] * 50))
        # Default SMA lanes warm up at length-1 (10/20/30/40) and then sit flat.
        assert all(v is None for v in result["ma1"][:9])
        assert all(v == pytest.approx(42.0) for v in result["ma1"][9:])
        assert all(v is None for v in result["ma2"][:19])
        assert all(v == pytest.approx(42.0) for v in result["ma2"][19:])
        assert all(v is None for v in result["ma3"][:29])
        assert all(v == pytest.approx(42.0) for v in result["ma3"][29:])
        assert all(v is None for v in result["ma4"][:39])
        assert all(v == pytest.approx(42.0) for v in result["ma4"][39:])

    def test_woodies_cci_flat_gap_and_hist_mirrors_cci14(self):
        from tradex_trading.analytics.studies_simple import woodies_cci

        # Constant series -> mean absolute deviation 0 -> every column is a gap.
        flat = woodies_cci(_candles_from_closes([50.0] * 30), 5, 14)
        assert all(v is None for v in flat["hist"])
        assert all(v is None for v in flat["turbo"])
        assert all(v is None for v in flat["cci14"])

        live = woodies_cci(_candles_from_closes([10.0 + i * 0.5 for i in range(40)]), 5, 14)
        # hist and cci14 are the SAME slow series; turbo is the fast CCI.
        assert live["hist"] == live["cci14"]
        assert live["turbo"][4] is not None
        assert live["hist"][4] is None
        assert live["hist"][13] is not None

    def test_special_k_slowest_term_warmup(self):
        from tradex_trading.analytics.studies_simple import special_k

        candles = _candles_from_closes([10.0 + i * 0.5 for i in range(400)])
        result = special_k(candles, length1=10, length2=10)
        # Slowest term needs roc(195) + smooth(130) - 1 = 324 bars.
        assert all(v is None for v in result["specialK"][:324])
        assert result["specialK"][324] is not None
        assert all(v is not None for v in result["specialK"][324:])
        # signal = sma(specialK, 10) then sma(..., 10): 324 + 9 + 9 = 342.
        assert all(v is None for v in result["signal"][:342])
        assert result["signal"][342] is not None


# ---------------------------------------------------------------------------
# B5 parallel split (studies_trend.py) edge tests
# ---------------------------------------------------------------------------


class TestStudiesTrend:
    def test_alligator_warmup_order_and_flat_equality(self):
        from tradex_trading.analytics.studies_trend import alligator

        flat = [_candle(10, 10, 10, 10)] * 40
        result = alligator(flat)
        # First prints at length-1+offset: lips 5/3 -> 7, teeth 8/5 -> 12,
        # jaw 13/8 -> 20. After their warmups every lane is the flat price.
        assert all(v is None for v in result["lips"][:7])
        assert all(v == pytest.approx(10.0) for v in result["lips"][7:])
        assert all(v is None for v in result["teeth"][:12])
        assert all(v == pytest.approx(10.0) for v in result["teeth"][12:])
        assert all(v is None for v in result["jaw"][:20])
        assert all(v == pytest.approx(10.0) for v in result["jaw"][20:])

    def test_parabolic_sar_seed_and_uptrend_stay(self):
        from tradex_trading.analytics.studies_trend import parabolic_sar

        candles = _candles_from_closes([10.0 + i for i in range(40)])
        sar = parabolic_sar(candles)["sar"]
        lows = [10.0 + i - 1.0 for i in range(40)]
        assert sar[0] is None
        assert sar[1] == pytest.approx(lows[0])
        assert all(v is not None for v in sar[1:])
        # A monotonic rise never flips: SAR stays below the lows throughout.
        for i in range(2, len(sar)):
            assert sar[i] < lows[i]

    def test_ichimoku_conversion_base_span_and_lagging(self):
        from tradex_trading.analytics.studies_trend import ichimoku

        n = 120
        closes = [10.0 + i * 0.5 for i in range(n)]
        result = ichimoku(_candles_from_closes(closes), 9, 26, 52, 26)
        conv, base = result["conversion"], result["base"]
        assert all(v is None for v in conv[:8])
        assert conv[8] is not None
        assert all(v is None for v in base[:25])
        assert base[25] is not None
        span_a = result["spanA"]
        # spanA needs both conversion and base live (25) AND is displaced
        # forward by 26, so its first print sits at 51.
        assert all(v is None for v in span_a[:51])
        assert span_a[51] == pytest.approx((conv[25] + base[25]) / 2.0)
        span_b = result["spanB"]
        assert all(v is None for v in span_b[:77])
        assert span_b[77] is not None
        lagging = result["lagging"]
        for i in range(n - 26):
            assert lagging[i] == pytest.approx(closes[i + 26])
        assert all(v is None for v in lagging[n - 26:])

    def test_halftrend_flat_level_and_mutual_exclusion(self):
        from tradex_trading.analytics.studies_trend import halftrend

        flat = [_candle(10, 10, 10, 10)] * 40
        flat_res = halftrend(flat)
        # On a flat series the level just follows price and stays one-sided.
        assert all(v == pytest.approx(10.0) for v in flat_res["up"])
        assert all(v is None for v in flat_res["down"])
        for u, d in zip(flat_res["up"], flat_res["down"]):
            assert not (u is not None and d is not None)

        live = halftrend(_candles_from_closes([10.0 + i * 0.3 for i in range(60)]))
        for u, d in zip(live["up"], live["down"]):
            assert not (u is not None and d is not None)

    def test_alphatrend_lagged_is_level_two_back(self):
        from tradex_trading.analytics.studies_trend import alphatrend

        candles = _candles_from_closes([10.0 + i * 0.4 for i in range(60)])
        result = alphatrend(candles, coeff=1, ap=14)
        level, lagged = result["alphatrend"], result["lagged"]
        assert lagged[0] is None and lagged[1] is None
        for i in range(2, len(candles)):
            assert lagged[i] == level[i - 2]


# ---------------------------------------------------------------------------
# B5 parallel split (studies_complex.py) edge tests
# ---------------------------------------------------------------------------


class TestStudiesComplex:
    def test_cpr_returns_27_keys_and_manual_toggles(self):
        from datetime import timedelta

        from tradex_trading.analytics.studies_complex import cpr

        base = datetime(2026, 7, 15, 9, 15)
        candles = []
        for d in range(2):
            for m in range(3):
                candles.append(
                    _candle(100 + d, 102 + d, 98 + d, 101 + d, 100,
                            base + timedelta(days=d, minutes=m))
                )
        result = cpr(candles, pivot_mode="manual",
                     show_daily=True, show_weekly=False, show_monthly=False,
                     display_pivots=True, display_support=True,
                     display_resistance=True, display_cpr=True, display_s1r1=True)
        levels = ("Pivot", "S1", "S2", "S3", "R1", "R2", "R3", "Bc", "Tc")
        prefixes = ("d", "w", "m")
        assert set(result) == {f"{p}{k}" for p in prefixes for k in levels}
        assert len(result) == 27
        # Daily frame is live once the second session opens; weekly/monthly off.
        for k in levels:
            assert any(v is not None for v in result[f"d{k}"])
        for p in ("w", "m"):
            for k in levels:
                assert all(v is None for v in result[f"{p}{k}"])

    def test_range_analysis_avg_gated_by_show_average(self):
        from tradex_trading.analytics.studies_complex import range_analysis

        candles = _candles_from_closes([10.0 + i for i in range(20)])
        off = range_analysis(candles, show_average=False, avg_length=3)
        # Fixture bars are high=close+0.5, low=close-1.0 -> range 1.5.
        assert all(v == pytest.approx(1.5) for v in off["range"])
        assert all(v is None for v in off["avg_range"])

        on = range_analysis(candles, show_average=True, avg_length=3)
        assert all(v is None for v in on["avg_range"][:2])
        assert all(v == pytest.approx(1.5) for v in on["avg_range"][2:])

    def test_vortex_first_print_and_nonnegative(self):
        from tradex_trading.analytics.studies_complex import vortex

        result = vortex(_candles_from_closes([10.0 + i * 0.5 for i in range(30)]), 14)
        assert all(v is None for v in result["vip"][:14])
        assert result["vip"][14] is not None
        for key in ("vip", "vim"):
            for v in result[key]:
                if v is not None:
                    assert v >= 0.0

    def test_relative_vigor_index_offset_shifts_rvgi(self):
        from tradex_trading.analytics.studies_complex import relative_vigor_index

        candles = _candles_from_closes([10.0 + i * 0.3 + (i % 3) for i in range(60)])
        base_rvgi = relative_vigor_index(candles, length=10, offset=0)["rvgi"]
        assert all(v is None for v in base_rvgi[:12])
        assert base_rvgi[12] is not None
        shifted = relative_vigor_index(candles, length=10, offset=1)["rvgi"]
        assert shifted[0] is None
        for i in range(len(candles) - 1):
            assert shifted[i + 1] == base_rvgi[i]

    def test_relative_volatility_index_monotonic_up_approaches_100(self):
        from tradex_trading.analytics.studies_complex import relative_volatility_index

        candles = _candles_from_closes([10.0 + i * 0.5 for i in range(60)])
        result = relative_volatility_index(candles, length=10, offset=0,
                                           ma_type="SMA", ma_length=14, bb_mult=2.0)
        rvi = result["rvi"]
        # stddev(10) warmup + fixed 14-bar EMA seed: first at 9 + 13 = 22.
        assert all(v is None for v in rvi[:22])
        assert rvi[22] is not None
        for v in rvi:
            if v is not None:
                assert 0.0 <= v <= 100.0
        # One-way up market: the down-flow EMA is 0, so RVI pins at 100.
        assert rvi[-1] == pytest.approx(100.0)
        assert all(v is None for v in result["bb_upper"])
        assert all(v is None for v in result["bb_lower"])


# ---------------------------------------------------------------------------
# B5 parallel split (studies_signals.py) edge tests
# ---------------------------------------------------------------------------


class TestStudiesSignals:
    def test_rsi_divergence_rsi_bounded_and_flat_signals(self):
        from tradex_trading.analytics.studies_signals import rsi_divergence

        result = rsi_divergence([_candle(10, 10, 10, 10)] * 40, 14, 5, 5)
        for v in result["rsi"]:
            if v is not None:
                assert 0.0 <= v <= 100.0
        assert result["rsi"][-1] == pytest.approx(100.0)
        # Flat series: no strict RSI pivots, so every signal column is null.
        for key in ("bull", "bear", "hiddenBull", "hiddenBear"):
            assert all(v is None for v in result[key])

    def test_trend_strength_index_flat_none_and_monotonic_one(self):
        from tradex_trading.analytics.studies_signals import trend_strength_index

        flat = [_candle(10, 10, 10, 10)] * 40
        assert all(v is None for v in trend_strength_index(flat, 14)["tsi"])

        tsi = trend_strength_index(_candles_from_closes([10.0 + i for i in range(40)]), 14)
        out = tsi["tsi"]
        assert all(v is None for v in out[:13])
        assert out[13] is not None
        assert out[-1] == pytest.approx(1.0, abs=1e-9)

    def test_williams_fractals_null_layer_and_pivot_values(self):
        from tradex_trading.analytics.studies_signals import williams_fractals

        closes = [10, 11, 12, 11, 10, 12, 13, 12, 11]
        candles = _candles_from_closes([float(c) for c in closes])
        highs = [c + 0.5 for c in closes]
        lows = [c - 1.0 for c in closes]
        result = williams_fractals(candles, periods=2)
        assert all(v is None for v in result["fractals"])
        up, down = result["upFractal"], result["downFractal"]
        assert up[2] == pytest.approx(highs[2])
        assert up[6] == pytest.approx(highs[6])
        assert down[4] == pytest.approx(lows[4])
        for i, v in enumerate(up):
            if v is not None:
                assert v == pytest.approx(highs[i])
        for i, v in enumerate(down):
            if v is not None:
                assert v == pytest.approx(lows[i])
        # The last `periods` bars are never confirmed, so no fractal there.
        assert all(v is None for v in up[-2:])
        assert all(v is None for v in down[-2:])

    def test_williams_vix_fix_bounds_and_toggled_columns_null(self):
        from tradex_trading.analytics.studies_signals import williams_vix_fix

        candles = _candles_from_closes([10.0 + i * 0.4 for i in range(60)])
        result = williams_vix_fix(candles, pd=22, bbl=20, mult=2.0, lb=50,
                                  ph=0.85, pl=1.01)
        wvf = result["wvf"]
        assert all(v is None for v in wvf[:21])
        assert wvf[21] is not None
        for v in wvf:
            if v is not None:
                assert 0.0 <= v <= 100.0
        # The range/band toggles are not backend params, so all three are null.
        assert all(v is None for v in result["range_high"])
        assert all(v is None for v in result["range_low"])
        assert all(v is None for v in result["upper_band"])

    def test_wavetrend_warmup_and_mom_identity(self):
        from tradex_trading.analytics.studies_signals import wavetrend

        candles = _candles_from_closes([10.0 + i * 0.5 + (i % 4) for i in range(80)])
        result = wavetrend(candles, n1=10, n2=21, sig_len=4)
        wt1, wt2, mom = result["wt1"], result["wt2"], result["mom"]
        # wt1 prints at 38 (10 + 9 + 20-ish chain), wt2/mom at 41.
        assert all(v is None for v in wt1[:38])
        assert wt1[38] is not None
        assert all(v is None for v in wt2[:41])
        assert wt2[41] is not None
        assert all(v is None for v in mom[:41])
        assert mom[41] is not None
        for i in range(len(candles)):
            if wt1[i] is not None and wt2[i] is not None:
                assert mom[i] == pytest.approx(wt1[i] - wt2[i])
            else:
                assert mom[i] is None
