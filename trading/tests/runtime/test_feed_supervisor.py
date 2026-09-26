"""Contract tests for feed health and recovery state."""

from __future__ import annotations

from datetime import UTC, datetime

from tradex_trading.runtime.feed_supervisor import (
    FeedState,
    FeedSupervisor,
    RecoveryResult,
)


def test_feed_is_not_ready_until_recovery_succeeds() -> None:
    supervisor = FeedSupervisor()

    supervisor.connected()
    supervisor.recovery_started()

    assert supervisor.state is FeedState.RESYNCHRONIZING
    assert not supervisor.ready

    result = supervisor.recovery_succeeded(
        last_event_at=datetime(2026, 9, 24, 10, 0, tzinfo=UTC),
        recovered_through=datetime(2026, 9, 24, 10, 1, tzinfo=UTC),
    )

    assert result.ok is True
    assert supervisor.state is FeedState.READY
    assert supervisor.ready is True
    assert supervisor.generation == 1


def test_failed_recovery_halts_feed() -> None:
    supervisor = FeedSupervisor()

    supervisor.connected()
    supervisor.recovery_started()
    result = supervisor.recovery_failed("history gap")

    assert isinstance(result, RecoveryResult)
    assert result.ok is False
    assert result.error == "history gap"
    assert supervisor.state is FeedState.HALTED
    assert supervisor.ready is False


def test_disconnect_degrades_a_ready_feed() -> None:
    supervisor = FeedSupervisor()
    supervisor.connected()
    supervisor.recovery_started()
    supervisor.recovery_succeeded(
        last_event_at=datetime(2026, 9, 24, 10, 0, tzinfo=UTC),
        recovered_through=datetime(2026, 9, 24, 10, 1, tzinfo=UTC),
    )

    supervisor.disconnected()

    assert supervisor.state is FeedState.DEGRADED
    assert supervisor.ready is False


def test_bar_frame_health_metadata_is_additive() -> None:
    from tradex_trading.runtime.bar_aggregator import BarFrame

    frame = BarFrame(
        instrument="NSE:TEST",
        timeframe="1m",
        time=1,
        open=1.0,
        high=1.0,
        low=1.0,
        close=1.0,
        volume=1.0,
        closed=False,
        feed_id="test-feed",
        connection_generation=3,
        quality_flags=("provisional",),
    )

    assert frame.feed_id == "test-feed"
    assert frame.connection_generation == 3
    assert frame.quality_flags == ("provisional",)

    legacy = BarFrame("NSE:TEST", "1m", 1, 1.0, 1.0, 1.0, 1.0, 1.0, True)
    assert legacy.feed_id is None
    assert legacy.connection_generation is None
