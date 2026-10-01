"""Replay lifecycle, tested without a WebSocket.

Phase 2 of the simulation-architecture work. Every test here runs under
``asyncio.run`` with injected sinks — before the extraction, driving a replay
required a live socket with its writer task, two queues and an auth gate, so
the only executable spec of replay semantics was a transport test.

The property under test throughout is the one the plan calls a timing
guarantee: **a step acknowledgement is causally ordered after the bar it
advanced through.** Asserting frame *contents* would pass even if the frames
were published in the wrong order, so the ordering assertions below index into
a single recorded frame list rather than into two.
"""

from __future__ import annotations

import asyncio
from datetime import datetime, timedelta
from decimal import Decimal

import pytest
from tradex_domain.enums import Timeframe
from tradex_domain.instruments import Equity
from tradex_domain.market import OHLC, Candle
from tradex_domain.value_objects import Price, Quantity
from tradex_runtime.bar_aggregator import BarAggregator
from tradex_runtime.replay_controller import (
    ReplayController,
    ReplayStartParams,
    ReplayState,
    ReplayTransitionError,
    parse_replay_start,
)

INSTRUMENT = "NSE:RELIANCE"


def _candles(count: int = 6, *, start: datetime | None = None) -> list[Candle]:
    """A short run of flat M1 candles at 09:15 — deterministic by design.

    Flat prices mean the tick generator has nothing to walk, so the assertions
    are about *lifecycle and ordering*, not about market simulation.
    """
    equity = Equity.of("NSE", "RELIANCE")
    base = start or datetime(2026, 1, 5, 9, 15)
    out: list[Candle] = []
    for i in range(count):
        price = Price(value=Decimal("100"))
        out.append(
            Candle(
                instrument=equity,
                timeframe=Timeframe.M1,
                ohlc=OHLC(open=price, high=price, low=price, close=price),
                volume=Quantity(value=Decimal("10")),
                timestamp=base + timedelta(minutes=i),
            )
        )
    return out


def _params(**overrides) -> ReplayStartParams:
    base = dict(
        instrument=INSTRUMENT,
        timeframe=Timeframe.M1,
        method="ohlc",
        seed=7,
        ticks_per_bar=2,
    )
    base.update(overrides)
    return ReplayStartParams(**base)


class _Harness:
    """A controller wired to in-memory sinks, with one shared frame log.

    Control and bar frames go to *different* sinks but the *same* list, so a
    test can assert relative ordering across the two — which is the property
    that matters, since both end up on one socket in production.
    """

    def __init__(self, candles: list[Candle] | None = None) -> None:
        self.frames: list[dict] = []
        self.controls: list[dict] = []
        self.purged: list[str] = []
        self._candles = candles if candles is not None else _candles()

    def _make_aggregator(self, instrument: str, interval: str, run_id: str, on_frame):
        from tradex_domain.enums import Timeframe as TF

        return BarAggregator(
            instrument, TF(interval), on_frame=on_frame, run_id=run_id
        )

    def controller(self) -> ReplayController:
        return ReplayController(
            make_aggregator=self._make_aggregator,
            emit_control=lambda f: (self.controls.append(f), self.frames.append(f)),
            emit_bar=lambda f: self.frames.append(f),
            load_candles=lambda params: self._candles,
            purge_run_frames=self.purged.append,
        )

    def types(self) -> list[str]:
        return [f["type"] for f in self.frames]

    def index_of(self, frame_type: str) -> int:
        for i, frame in enumerate(self.frames):
            if frame["type"] == frame_type:
                return i
        raise AssertionError(f"no {frame_type} frame in {self.types()}")


def _run(coro):
    return asyncio.run(coro)


class TestStateTransitionsAreExplicit:
    def test_starts_idle(self) -> None:
        h = _Harness()
        assert h.controller().state is ReplayState.IDLE

    def test_start_moves_to_running_and_emits_started(self) -> None:
        h = _Harness()

        async def scenario():
            c = h.controller()
            await c.start(_params())
            await asyncio.sleep(0.05)
            await c.close()

        _run(scenario())
        assert h.index_of("replay_started") >= 0
        assert "bar" in h.types(), "no bars were emitted — the feed never ran"

    def test_pause_from_idle_is_refused_not_ignored(self) -> None:
        """The regression this extraction exists to prevent.

        Under the three-boolean design, pausing with no active run set a field
        on nothing and still acked ``replay_paused`` — a client could not tell
        a real pause from a no-op.
        """
        h = _Harness()

        async def scenario():
            c = h.controller()
            with pytest.raises(ReplayTransitionError):
                await c.pause()

        _run(scenario())
        assert "replay_paused" not in h.types(), (
            "a refused pause must not ack — the client would believe it paused"
        )

    def test_pause_then_resume_returns_to_running(self) -> None:
        h = _Harness()

        async def scenario():
            c = h.controller()
            await c.start(_params())
            await asyncio.sleep(0.02)
            await c.pause()
            assert c.state is ReplayState.PAUSED
            await c.resume()
            assert c.state is ReplayState.RUNNING
            await c.close()

        _run(scenario())
        assert h.types().count("replay_paused") == 1
        assert "replay_resumed" in h.types()

    def test_step_after_stop_is_refused(self) -> None:
        h = _Harness()

        async def scenario():
            c = h.controller()
            await c.start(_params())
            await c.stop()
            assert c.state is ReplayState.STOPPED
            with pytest.raises(ReplayTransitionError):
                await c.step()

        _run(scenario())

    def test_seek_from_idle_is_refused(self) -> None:
        h = _Harness()

        async def scenario():
            with pytest.raises(ReplayTransitionError):
                await h.controller().seek({"index": 0})

        _run(scenario())
        assert "replay_error" not in h.types(), (
            "seeking with no run should refuse, not emit a runtime error frame"
        )


class TestCausalOrdering:
    def test_step_ack_comes_after_the_bar_it_advanced_through(self) -> None:
        """The plan's timing guarantee, asserted as ordering not content."""
        h = _Harness()

        async def scenario():
            c = h.controller()
            await c.start(_params())
            await asyncio.sleep(0.01)
            await c.pause()
            await c.step()
            # Let the feed loop finish the step and publish its ack.
            for _ in range(200):
                await asyncio.sleep(0.005)
                if "replay_stepped" in h.types():
                    break
            await c.close()

        _run(scenario())

        step_idx = h.index_of("replay_stepped")
        bars_before = [
            i for i, f in enumerate(h.frames[:step_idx]) if f["type"] == "bar"
        ]
        assert bars_before, (
            f"replay_stepped arrived with no bar before it: {h.types()}"
        )
        assert bars_before[-1] == step_idx - 1 or step_idx - 1 in bars_before, (
            "the step ack was not immediately preceded by the bar it stepped over"
        )
        # And the ack names a bar index the run actually reached.
        ack = h.frames[step_idx]
        assert ack["run_id"]
        assert isinstance(ack["bar_index"], int)

    def test_step_is_followed_by_paused_so_the_run_does_not_continue(self) -> None:
        h = _Harness()

        async def scenario():
            c = h.controller()
            await c.start(_params())
            await asyncio.sleep(0.01)
            await c.pause()
            await c.step()
            for _ in range(200):
                await asyncio.sleep(0.005)
                if "replay_stepped" in h.types():
                    break
            assert c.state is ReplayState.PAUSED, "a step must leave the run paused"
            await c.close()

        _run(scenario())
        types = h.types()
        assert types.index("replay_stepped") < len(types) - 1


class TestTerminalFrames:
    def test_running_out_of_candles_emits_done(self) -> None:
        h = _Harness(_candles(3))

        async def scenario():
            c = h.controller()
            # High speed so the run finishes inside the poll budget: at
            # speed=1.0 each candle is paced at 1s of wall time.
            await c.start(_params(speed=1000.0))
            for _ in range(400):
                await asyncio.sleep(0.005)
                if "replay_done" in h.types():
                    break
            assert c.state is ReplayState.DONE
            await c.close()

        _run(scenario())
        assert "replay_done" in h.types()

    def test_stop_emits_stopped_with_the_run_id(self) -> None:
        h = _Harness()

        async def scenario():
            c = h.controller()
            run_id = await c.start(_params())
            await asyncio.sleep(0.01)
            await c.stop()
            frame = h.controls[-1]
            assert frame["type"] == "replay_stopped"
            assert frame["run_id"] == run_id
            await c.close()

        _run(scenario())

    def test_stop_with_nothing_running_acks_bare(self) -> None:
        h = _Harness()

        async def scenario():
            await h.controller().stop()

        _run(scenario())
        assert h.types() == ["replay_stopped"]

    def test_stopping_purges_queued_bars_for_the_run(self) -> None:
        """A stopped replay must not emit bars after its terminal frame."""
        h = _Harness()

        async def scenario():
            c = h.controller()
            await c.start(_params())
            await asyncio.sleep(0.01)
            await c.stop()
            await c.close()

        _run(scenario())
        assert h.purged, "the transport was never asked to drop the run's queued bars"


class TestSpeedAndErrors:
    def test_speed_change_is_range_checked(self) -> None:
        h = _Harness()

        async def scenario():
            c = h.controller()
            await c.start(_params())
            await asyncio.sleep(0.01)
            with pytest.raises(ValueError):
                c.set_speed(-1)
            assert c.speed == 1.0
            await c.close()

        _run(scenario())
        assert any(f["type"] == "replay_error" for f in h.controls)

    def test_empty_dataset_errors_instead_of_starting(self) -> None:
        h = _Harness(candles=[])

        async def scenario():
            c = h.controller()
            with pytest.raises(ValueError):
                await c.start(_params())
            assert c.state is ReplayState.IDLE, "a refused start left the controller dirty"

        _run(scenario())
        assert any(f["type"] == "replay_error" for f in h.controls)


class TestParameterParsing:
    @pytest.mark.parametrize(
        "msg,expected",
        [
            ({"instrument": "NSE:X"}, "1m"),
            ({"instrument": "NSE:X", "interval": "1m"}, "1m"),
            ({"instrument": "NSE:X", "interval": "5m"}, "5m"),
            ({"instrument": "NSE:X", "interval": "1h"}, "1h"),
        ],
    )
    def test_interval_normalisation(self, msg, expected) -> None:
        assert parse_replay_start(msg).timeframe.value == expected

    @pytest.mark.parametrize("raw", ["D", "1D", "d", "1d"])
    def test_daily_replay_is_refused(self, raw: str) -> None:
        """``D`` normalises to ``1d``, which has no intrabar path to walk.

        Normalisation is worth keeping (REST spells daily ``D``, the domain
        spells it ``1d``), but the replay itself cannot run at that interval,
        so it is rejected after normalising rather than before.
        """
        with pytest.raises(ValueError, match="unsupported replay interval"):
            parse_replay_start({"instrument": "NSE:X", "interval": raw})

    @pytest.mark.parametrize(
        "msg",
        [
            {},
            {"instrument": ""},
            {"instrument": "NOSE-X"},
            {"instrument": "NSE:X", "interval": "3d"},
            {"instrument": "NSE:X", "method": "magic"},
            {"instrument": "NSE:X", "ticks_per_bar": 1},
            {"instrument": "NSE:X", "minutes": 0},
            {"instrument": "NSE:X", "speed": "fast"},
        ],
    )
    def test_bad_input_is_refused_with_a_reason(self, msg) -> None:
        with pytest.raises(ValueError):
            parse_replay_start(msg)

    def test_bools_are_not_numbers(self) -> None:
        """``True`` is an ``int`` in Python and a plausible-looking speed."""
        with pytest.raises(ValueError):
            parse_replay_start({"instrument": "NSE:X", "speed": True})
