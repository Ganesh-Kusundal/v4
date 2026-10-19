from tradex_domain.enums import Timeframe
from tradex_domain.timeframe import normalize, to_timeframe


def test_normalize_interval_daily():
    for raw in ("1d", "D", "1D", "d"):
        assert to_timeframe(raw) is Timeframe.D1
        assert normalize(raw) == ("1d", Timeframe.D1)


def test_normalize_interval_passthrough():
    assert normalize("1m") == ("1m", Timeframe.M1)
    assert normalize("5m") == ("5m", Timeframe.M5)
    assert normalize("15m") == ("15m", Timeframe.M15)
    assert normalize("30m") == ("30m", Timeframe.M30)
    assert normalize("1h") == ("1h", Timeframe.H1)
    assert to_timeframe("D") is Timeframe.D1
