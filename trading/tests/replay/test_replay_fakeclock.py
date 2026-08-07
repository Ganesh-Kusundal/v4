"""Tests for replay backtest — FakeClock and BacktestEngine.submit.

Tests the ported FakeClock class and BacktestEngine.submit method.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

from tradex_trading.replay.backtest import BacktestEngine, FakeClock

# ---------------------------------------------------------------------------
# FakeClock
# ---------------------------------------------------------------------------


class TestFakeClock:
    """FakeClock — deterministic clock for backtesting."""

    def test_default_start_is_now(self) -> None:
        """Default start time should be close to current UTC time."""
        before = datetime.now(UTC)
        clock = FakeClock()
        now = clock.now()
        after = datetime.now(UTC)
        # Allow small tolerance for timing
        assert before - timedelta(seconds=1) <= now <= after + timedelta(seconds=1)

    def test_explicit_start_time(self) -> None:
        """Should start at the specified time."""
        start = datetime(2026, 1, 1, 12, 0, tzinfo=UTC)
        clock = FakeClock(start=start)
        assert clock.now() == start

    def test_advance_by_timedelta(self) -> None:
        """advance() should move time forward by the given delta."""
        start = datetime(2026, 1, 1, 12, 0, tzinfo=UTC)
        clock = FakeClock(start=start)

        clock.advance(timedelta(minutes=5))
        assert clock.now() == start + timedelta(minutes=5)

    def test_multiple_advances(self) -> None:
        """Multiple advances should accumulate."""
        start = datetime(2026, 1, 1, 12, 0, tzinfo=UTC)
        clock = FakeClock(start=start)

        clock.advance(timedelta(minutes=5))
        clock.advance(timedelta(minutes=10))
        clock.advance(timedelta(hours=1))

        expected = start + timedelta(minutes=5) + timedelta(minutes=10) + timedelta(hours=1)
        assert clock.now() == expected

    def test_advance_by_zero(self) -> None:
        """Advancing by zero should not change time."""
        start = datetime(2026, 1, 1, 12, 0, tzinfo=UTC)
        clock = FakeClock(start=start)

        clock.advance(timedelta(0))
        assert clock.now() == start


# ---------------------------------------------------------------------------
# BacktestEngine.submit
# ---------------------------------------------------------------------------


class TestBacktestEngineSubmit:
    """BacktestEngine.submit — route orders through the engine."""

    def test_submit_returns_request_when_no_fill_source(self) -> None:
        """submit() should return the request when no fill source is set."""
        engine = BacktestEngine()
        request = {"symbol": "RELIANCE", "side": "BUY", "quantity": 1}
        result = engine.submit(request)
        assert result == request

    def test_submit_with_fill_source(self) -> None:
        """submit() should delegate to fill source if available."""

        class MockFillSource:
            def submit(self, req):
                return {"status": "FILLED", "request": req}

        engine = BacktestEngine(fill_source=MockFillSource())
        request = {"symbol": "RELIANCE", "side": "BUY"}
        result = engine.submit(request)
        assert result["status"] == "FILLED"
        assert result["request"] == request

    def test_submit_with_fill_source_without_submit_method(self) -> None:
        """submit() should return request if fill source has no submit method."""
        engine = BacktestEngine(fill_source="not_a_real_source")
        request = {"symbol": "RELIANCE"}
        result = engine.submit(request)
        assert result == request

    def test_engine_accepts_clock(self) -> None:
        """BacktestEngine should accept a FakeClock."""
        start = datetime(2026, 1, 1, 12, 0, tzinfo=UTC)
        clock = FakeClock(start=start)
        engine = BacktestEngine(clock=clock)
        assert engine._clock.now() == start

    def test_engine_creates_default_clock(self) -> None:
        """BacktestEngine should create a default FakeClock if none provided."""
        engine = BacktestEngine()
        assert isinstance(engine._clock, FakeClock)
