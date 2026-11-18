"""Pin the seeded EMA / VWMA used by the studies, so sharing the `_math` copy
cannot change output (gap and zero-volume cases included)."""

from tradex_analytics import _math
from tradex_analytics.studies import studies_complex, studies_simple

_CLOSES = [10.0, 11.0, 12.0, 13.0, 12.5, 14.0, 15.0, 14.5, 16.0, 17.0]
_GAPPED = [1.0, 2.0, 3.0, None, 5.0, 6.0, 7.0, 8.0, 9.0, 10.0]
_VOLS = [float(i + 1) for i in range(10)]
_VOLS[4] = 0.0


def test_seeded_ema_pinned():
    assert studies_complex._seeded_ema(_CLOSES, 3) == [
        None, None, 11.0, 12.0, 12.25, 13.125, 14.0625, 14.28125, 15.140625, 16.0703125,
    ]
    assert studies_complex._seeded_ema(_GAPPED, 3) == [
        None, None, 2.0, None, None, None, 6.0, 7.0, 8.0, 9.0,
    ]


def test_vwma_pinned():
    got = studies_simple._vwma(_CLOSES, _VOLS, 3)
    assert got[:2] == [None, None]
    assert abs(got[2] - 11.333333333333334) < 1e-12
    assert abs(got[-1] - 15.925925925925927) < 1e-12


def test_studies_share_the_math_helpers():
    assert studies_complex._seeded_ema is _math._seeded_ema
    assert studies_simple._vwma is _math._vwma
