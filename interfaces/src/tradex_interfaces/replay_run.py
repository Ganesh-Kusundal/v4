from __future__ import annotations

import asyncio
from dataclasses import dataclass
from datetime import datetime

from tradex_domain.enums import Timeframe
from tradex_domain.market import Candle
from tradex_domain.timezones import IST_ZONE as _IST

from tradex_runtime.bar_aggregator import BarAggregator, _tf_seconds


def _chart_seconds(timestamp: datetime) -> int:
    if timestamp.tzinfo is None:
        timestamp = timestamp.replace(tzinfo=_IST)
    else:
        timestamp = timestamp.astimezone(_IST)
    return int(timestamp.timestamp())


def _target_key(timestamp: datetime, timeframe: Timeframe) -> int:
    seconds = _tf_seconds(timeframe)
    return _chart_seconds(timestamp) - (_chart_seconds(timestamp) % seconds)


@dataclass
class ReplayRun:
    run_id: str
    instrument: str
    timeframe: Timeframe
    candles: tuple[Candle, ...]
    target_keys: tuple[int, ...]
    cursor: int = 0
    speed: float = 1.0
    paused: bool = False
    step_requested: bool = False
    aggregator: BarAggregator | None = None
    task: asyncio.Task[None] | None = None
    terminal: bool = False

    def __post_init__(self) -> None:
        if not isinstance(self.run_id, str) or not self.run_id.strip():
            raise ValueError("replay run id must be non-empty")
        if not isinstance(self.instrument, str) or not self.instrument.strip():
            raise ValueError("replay instrument must be non-empty")
        if not isinstance(self.timeframe, Timeframe):
            object.__setattr__(self, "timeframe", Timeframe(self.timeframe))
        object.__setattr__(self, "candles", tuple(self.candles))
        object.__setattr__(self, "target_keys", tuple(int(key) for key in self.target_keys))
        if self.cursor < 0:
            self.cursor = 0

    def target_index_for_key(self, key: int) -> int:
        needle = int(key)
        for index, candle in enumerate(self.candles):
            timestamp = getattr(candle, "timestamp", None)
            if timestamp is not None and _target_key(timestamp, self.timeframe) == needle:
                return index
        try:
            return self.target_keys.index(needle)
        except ValueError as exc:
            raise KeyError(needle) from exc

    def close(self) -> None:
        if self.terminal:
            return
        self.terminal = True
        self.paused = False
        self.step_requested = False
        aggregator = self.aggregator
        if aggregator is not None:
            try:
                aggregator.flush()
            finally:
                aggregator.dispose()

    async def cancel_and_drain(self) -> Exception | None:
        task = self.task
        self.task = None
        error: Exception | None = None
        current = asyncio.current_task()
        if task is not None and task is not current:
            if not task.done():
                task.cancel()
            try:
                await task
            except asyncio.CancelledError:
                pass
            except Exception as exc:
                error = exc
        self.close()
        return error


__all__ = ["ReplayRun"]
