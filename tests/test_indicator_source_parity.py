"""Zero parity: indicators must compute on bars regardless of source."""

from unittest.mock import patch

from tradex_domain.bar_source import BarSource


def test_bar_source_coerce():
    assert BarSource.coerce("sim") is BarSource.SIM
    assert BarSource.coerce("live") is BarSource.LIVE
    assert BarSource.coerce("backtest") is BarSource.BACKTEST
    assert BarSource.coerce("unknown") is BarSource.LIVE
    assert BarSource.coerce(BarSource.SIM) is BarSource.SIM


def test_indicators_compute_on_sim_bars():
    """A sim bar for a subscribed key must reach indicator computation."""
    from tradex_interfaces.routes.stream_indicators import IndicatorStreamRegistry

    registry = IndicatorStreamRegistry(lambda frame_dict: None)
    registry.add("RELIANCE", "1m", ["sma"], "bar-close")

    class FakeFrame:
        instrument = "RELIANCE"
        timeframe = "1m"
        closed = True
        source = "sim"
        time = 0

    with patch.object(registry, "_push_indicator") as mock_push:
        registry.handle_bar_frame(FakeFrame())
        mock_push.assert_called_once()


def test_indicators_compute_on_live_bars():
    """A live bar for a subscribed key must also reach computation."""
    from tradex_interfaces.routes.stream_indicators import IndicatorStreamRegistry

    registry = IndicatorStreamRegistry(lambda frame_dict: None)
    registry.add("RELIANCE", "1m", ["sma"], "bar-close")

    class FakeFrame:
        instrument = "RELIANCE"
        timeframe = "1m"
        closed = True
        source = "live"
        time = 0

    with patch.object(registry, "_push_indicator") as mock_push:
        registry.handle_bar_frame(FakeFrame())
        mock_push.assert_called_once()
