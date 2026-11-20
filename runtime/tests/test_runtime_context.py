"""``RuntimeContext`` resolves the run mode once into named predicates."""

from __future__ import annotations

from types import SimpleNamespace

from tradex_domain import Mode
from tradex_runtime.context import RuntimeContext


def _ctx(mode: Mode) -> RuntimeContext:
    return RuntimeContext.from_config(SimpleNamespace(mode=mode))


def test_predicates_match_each_mode() -> None:
    paper = _ctx(Mode.PAPER)
    assert paper.is_paper and paper.is_online and paper.needs_durable_guard
    assert not paper.is_live and not paper.threadsafe_bus

    live = _ctx(Mode.LIVE)
    assert live.is_live and live.is_online and live.needs_durable_guard
    assert live.threadsafe_bus

    backtest = _ctx(Mode.BACKTEST)
    assert backtest.is_backtest
    assert not backtest.is_online and not backtest.needs_durable_guard
    assert not backtest.threadsafe_bus

    replay = _ctx(Mode.REPLAY)
    assert replay.is_replay
    assert not replay.is_online and not replay.needs_durable_guard


def test_predicates_are_derived_from_mode() -> None:
    # Exactly one concrete mode predicate is true for any mode.
    for mode in Mode:
        ctx = _ctx(mode)
        concrete = [ctx.is_live, ctx.is_paper, ctx.is_backtest, ctx.is_replay]
        assert sum(concrete) == 1, mode


def test_fill_source_is_carried() -> None:
    ctx = RuntimeContext(mode=Mode.PAPER)
    assert ctx.fill_source is None
    ctx.fill_source = object()
    assert ctx.fill_source is not None
