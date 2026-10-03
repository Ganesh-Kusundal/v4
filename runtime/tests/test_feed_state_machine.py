"""FeedSupervisor state machine tests.

The supervisor owns the readiness state for one market data connection.
These tests pin the transitions that ``MarketFeed`` relies on: a feed that
recovers is ready, a feed that fails recovery is halted, a stale feed is
degraded, and a disconnected feed resets readiness.
"""

from __future__ import annotations

from datetime import UTC, datetime

import pytest
from tradex_runtime.feed_supervisor import FeedState, FeedSupervisor


class TestFeedStateTransitions:
    """The supervisor moves through states in a well-defined order."""

    def test_transitions_through_states(self) -> None:
        sup = FeedSupervisor()
        assert sup.state is FeedState.NEW
        assert not sup.ready

        sup.connected()
        assert sup.state is FeedState.CONNECTING
        assert not sup.ready

        sup.recovery_started()
        assert sup.state is FeedState.RESYNCHRONIZING
        assert not sup.ready

        result = sup.recovery_succeeded(
            last_event_at=datetime(2026, 7, 1, 9, 15, tzinfo=UTC),
            recovered_through=datetime(2026, 7, 1, 9, 15, tzinfo=UTC),
        )
        assert result.ok
        assert sup.state is FeedState.READY
        assert sup.ready
        assert sup.generation == 1

    def test_recovery_failure_demotes_ready(self) -> None:
        sup = FeedSupervisor()
        sup.connected()
        sup.recovery_started()
        sup.recovery_succeeded(
            last_event_at=datetime(2026, 7, 1, 9, 15, tzinfo=UTC),
            recovered_through=datetime(2026, 7, 1, 9, 15, tzinfo=UTC),
        )
        assert sup.ready

        result = sup.recovery_failed("broker timeout")
        assert not result.ok
        assert result.error == "broker timeout"
        assert sup.state is FeedState.HALTED
        assert not sup.ready

    def test_stale_marks_degraded(self) -> None:
        sup = FeedSupervisor()
        sup.connected()
        sup.recovery_started()
        sup.recovery_succeeded(
            last_event_at=datetime(2026, 7, 1, 9, 15, tzinfo=UTC),
            recovered_through=datetime(2026, 7, 1, 9, 15, tzinfo=UTC),
        )
        assert sup.ready

        sup.mark_stale()
        assert sup.state is FeedState.DEGRADED
        assert not sup.ready

    def test_disconnected_resets_readiness(self) -> None:
        sup = FeedSupervisor()
        sup.connected()
        sup.recovery_started()
        sup.recovery_succeeded(
            last_event_at=datetime(2026, 7, 1, 9, 15, tzinfo=UTC),
            recovered_through=datetime(2026, 7, 1, 9, 15, tzinfo=UTC),
        )
        assert sup.ready

        sup.disconnected()
        assert sup.state is FeedState.DEGRADED
        assert not sup.ready

    def test_halted_is_terminal(self) -> None:
        """Once halted, no further transitions change the state."""
        sup = FeedSupervisor()
        sup.connected()
        sup.recovery_started()
        sup.recovery_failed("fatal")
        assert sup.state is FeedState.HALTED

        sup.mark_stale()
        assert sup.state is FeedState.HALTED

        sup.disconnected()
        assert sup.state is FeedState.HALTED

    def test_recovery_result_carries_timestamps(self) -> None:
        sup = FeedSupervisor()
        sup.connected()
        sup.recovery_started()
        ts = datetime(2026, 7, 1, 9, 15, tzinfo=UTC)
        result = sup.recovery_succeeded(last_event_at=ts, recovered_through=ts)
        assert result.last_event_at == ts
        assert result.recovered_through == ts
        assert sup.last_event_at == ts
        assert sup.recovered_through == ts

    def test_record_event_updates_last_event_at(self) -> None:
        sup = FeedSupervisor()
        ts = datetime(2026, 7, 1, 10, 30, tzinfo=UTC)
        sup.record_event(ts)
        assert sup.last_event_at == ts

    def test_recovery_failed_with_empty_error_uses_default(self) -> None:
        sup = FeedSupervisor()
        sup.connected()
        sup.recovery_started()
        result = sup.recovery_failed("   ")
        assert result.error == "feed recovery failed"

    def test_generation_increments_on_each_connect(self) -> None:
        sup = FeedSupervisor()
        assert sup.generation == 0
        sup.connected()
        assert sup.generation == 1
        sup.connected()
        assert sup.generation == 2

    @pytest.mark.parametrize(
        "method",
        ["mark_stale", "disconnected"],
    )
    def test_degraded_transitions_do_not_affect_halted(self, method: str) -> None:
        sup = FeedSupervisor()
        sup.connected()
        sup.recovery_started()
        sup.recovery_failed("fatal")
        assert sup.state is FeedState.HALTED

        getattr(sup, method)()
        assert sup.state is FeedState.HALTED
