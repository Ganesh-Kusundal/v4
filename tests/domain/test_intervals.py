from tradex_domain.intervals import normalize_interval


def test_normalize_interval_daily():
    assert normalize_interval("1d") == "D"


def test_normalize_interval_passthrough():
    assert normalize_interval("1m") == "1m"
    assert normalize_interval("5m") == "5m"
    assert normalize_interval("15m") == "15m"
    assert normalize_interval("30m") == "30m"
    assert normalize_interval("1h") == "1h"
    assert normalize_interval("D") == "D"
