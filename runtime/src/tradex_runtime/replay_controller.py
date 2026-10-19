"""Replay lifecycle, owned by the runtime rather than by a transport adapter.

Phase 2 of the simulation-architecture work. Until now the replay state machine
lived inside the ``/ws/stream`` WebSocket closure: ``_replay_start``,
``_replay_pause``, ``_replay_resume``, ``_replay_step``, ``_replay_seek``,
``_replay_speed`` and ``_replay_stop`` were nested functions sharing mutable
cells (``replay_run``, ``replay_stop_ack_sent``, ``step_closed``,
``last_closed_bar_index``). Two problems with that ownership, both removed here
rather than papered over:

**The lifecycle was implicit.** ``ReplayRun`` carried ``paused`` /
``step_requested`` / ``terminal`` booleans and nothing else, so "is a replay
running, paused, or finished?" had no answer except by inspecting three flags in
a combination only the original author knew. Every transition was a direct field
write, so an illegal one — pause a stopped run, step a finished run, seek a run
with no dataset — was a silent no-op or a half-applied mutation rather than a
refusal. :class:`ReplayState` makes the state one enum value reachable only
through a validated transition.

**The state machine was untestable away from a socket.** Driving a replay
required a live ``WebSocket`` with its writer task, two queues and an auth gate,
so the only executable specification of replay semantics was a transport test.
This controller takes its transports as injected callables, so the same
lifecycle runs under ``asyncio.run`` with no socket at all
(``trading/tests/runtime/test_replay_controller.py``).

What deliberately stays in the route: the socket, the queues, auth, and
decisions that are genuinely per-connection (which instruments the client
wants, which aggregators exist). Those are not replay semantics.
"""

from __future__ import annotations

import asyncio
import enum
import logging
import math
import secrets
from collections.abc import Awaitable, Callable, Sequence
from dataclasses import dataclass
from typing import Any

from tradex_domain.enums import Timeframe
from tradex_domain.market import Candle
from tradex_domain.timeframe import TIMEFRAMES, candle_epoch, interval_seconds, to_timeframe
from tradex_domain.timezones import IST_ZONE

log = logging.getLogger(__name__)

#: Sink for one outbound frame. ``control`` frames must be non-lossy (the
#: transport closes the socket rather than dropping one); ``bar`` frames are
#: market data and may be evicted by policy.
EmitFn = Callable[[dict[str, Any]], None]


class ReplayState(enum.Enum):
    """The one authoritative replay lifecycle state.

    Explicit because the timing guarantee the plan asks for — "step/seek/replay
    acknowledgements are causally ordered against emitted bars" — is only
    enforceable if the controller knows what it is currently doing.

    The transitions the wire protocol may request:

    * ``IDLE -> CREATED``    — ``replay_start``
    * ``CREATED -> RUNNING`` — the feed task starts
    * ``RUNNING <-> PAUSED`` — ``replay_pause`` / ``replay_resume``
    * ``PAUSED -> RUNNING``  — ``replay_step`` (advances one target bar, re-pauses)
    * ``* -> STOPPED``       — ``replay_stop`` (from any non-idle state)
    * ``RUNNING -> DONE``    — the dataset ran out of candles

    ``IDLE`` ("no replay has been started") is deliberately distinct from
    ``STOPPED`` ("a replay ran and was stopped"), which is what lets
    ``replay_stop`` ack with a bare frame when nothing is active while still
    reporting a run id when something was.
    """

    IDLE = "idle"
    CREATED = "created"
    RUNNING = "running"
    PAUSED = "paused"
    STOPPED = "stopped"
    DONE = "done"
    ERROR = "error"


#: ``(from_state, command) -> to_state``. Anything absent is refused. Kept as
#: data rather than branches inside methods so the machine can be read — and
#: tested — as one table.
_TRANSITIONS: dict[tuple[ReplayState, str], ReplayState] = {
    (ReplayState.IDLE, "start"): ReplayState.CREATED,
    # A seek tears the old run down and starts a new one, so it is legal from
    # any state that can still hold a run.
    (ReplayState.CREATED, "seek"): ReplayState.CREATED,
    (ReplayState.RUNNING, "seek"): ReplayState.CREATED,
    (ReplayState.PAUSED, "seek"): ReplayState.CREATED,
    (ReplayState.STOPPED, "seek"): ReplayState.CREATED,
    (ReplayState.DONE, "seek"): ReplayState.CREATED,
    (ReplayState.ERROR, "seek"): ReplayState.CREATED,
    # ``stop`` is legal from every state that can hold a run; from IDLE there is
    # nothing to stop and the caller gets a bare ack instead of a transition.
    (ReplayState.CREATED, "stop"): ReplayState.STOPPED,
    (ReplayState.RUNNING, "stop"): ReplayState.STOPPED,
    (ReplayState.PAUSED, "stop"): ReplayState.STOPPED,
    (ReplayState.DONE, "stop"): ReplayState.STOPPED,
    (ReplayState.ERROR, "stop"): ReplayState.STOPPED,
    # Driven by the feed task itself.
    (ReplayState.CREATED, "run"): ReplayState.RUNNING,
    (ReplayState.RUNNING, "pause"): ReplayState.PAUSED,
    (ReplayState.RUNNING, "resume"): ReplayState.RUNNING,
    (ReplayState.PAUSED, "resume"): ReplayState.RUNNING,
    # A step advances and then re-pauses, so it leaves the run in PAUSED.
    (ReplayState.PAUSED, "step"): ReplayState.RUNNING,
    (ReplayState.RUNNING, "step"): ReplayState.RUNNING,
    # Terminal edges.
    (ReplayState.RUNNING, "finish"): ReplayState.DONE,
    (ReplayState.CREATED, "finish"): ReplayState.STOPPED,
    (ReplayState.PAUSED, "finish"): ReplayState.STOPPED,
    (ReplayState.ERROR, "finish"): ReplayState.STOPPED,
}

#: States in which a run exists and can still make progress.
_LIVE_STATES = frozenset({ReplayState.CREATED, ReplayState.RUNNING, ReplayState.PAUSED})
#: States with no outgoing edge except a fresh start/seek.
_TERMINAL_STATES = frozenset({ReplayState.STOPPED, ReplayState.DONE})

#: Upper bound on how long a step waits for its closed bar to be emitted
#: (100 x 10ms). A safety valve against a transport that never returns from
#: ``emit_bar``; the normal path returns on the first check.
_STEP_BAR_WAIT_TICKS = 100


class ReplayTransitionError(RuntimeError):
    """An illegal lifecycle transition was requested.

    Raised rather than swallowed so the caller can answer with a
    ``replay_error`` naming the refused move. Under the previous three-boolean
    design, "pause a finished replay" was indistinguishable from a pause that
    worked — the client saw a ``replay_paused`` ack either way.
    """


@dataclass(frozen=True)
class ReplayStartParams:
    """Validated ``replay_start`` parameters.

    Parsed and range-checked in the runtime rather than in the route, so the
    bounds live with the lifecycle they constrain and a second transport
    inherits them instead of reimplementing them.
    """

    instrument: str
    timeframe: Timeframe
    #: Default is ``ohlc`` — four deterministic prints that reconstruct the
    #: candle exactly. ``anchored`` / ``bridge`` are opt-in random walks.
    method: str = "ohlc"
    seed: int | None = None
    speed: float = 1.0
    ticks_per_bar: int = 10
    minutes: int = 390
    start_time: int | None = None
    #: Start the run already paused. Honoured rather than ignored: a client that
    #: asks for a paused replay wants to inspect the first bar before anything
    #: advances, and silently playing instead is a silent contract change.
    paused: bool = False

    @property
    def interval(self) -> str:
        return self.timeframe.value


def _bounded(raw: Any, name: str, default: float, low: float, high: float, *, as_int: bool) -> Any:
    """Coerce *raw* to a number inside ``[low, high]``, or raise ``ValueError``."""
    if raw is None:
        raw = default
    if isinstance(raw, bool):
        raise ValueError(f"{name} must be a number")
    try:
        value = float(raw)
    except (TypeError, ValueError) as exc:
        raise ValueError(f"{name} must be a number") from exc
    if not math.isfinite(value):
        raise ValueError(f"{name} must be a finite number")
    if not low <= value <= high:
        raise ValueError(f"{name} must be between {low:g} and {high:g}")
    if as_int:
        if value != int(value):
            raise ValueError(f"{name} must be an integer")
        return int(value)
    return value


def parse_replay_start(msg: dict[str, Any]) -> ReplayStartParams:
    """Parse and range-check a ``replay_start`` message.

    Rejects here what a later stage would fail on anyway, naming the field —
    so the client learns about a bad ``speed`` rather than about a replay that
    "didn't start".
    """
    raw_instrument = msg.get("instrument")
    if not isinstance(raw_instrument, str) or not raw_instrument.strip():
        raise ValueError("replay_start needs 'instrument'")
    instrument = raw_instrument.strip()
    if ":" not in instrument or any(not part for part in instrument.split(":", 1)):
        raise ValueError("replay instrument must be EXCHANGE:SYMBOL")

    raw_interval = str(msg.get("interval", "1m"))
    # One normalization path (domain/timeframe): "D"/"1D"/"d"/"1d" all land
    # on Timeframe.D1, which the exclusion below then refuses — a daily
    # replay has no intrabar path to walk, and it must be refused after
    # normalising, not before.
    try:
        timeframe = to_timeframe(raw_interval)
    except ValueError as exc:
        raise ValueError(f"unsupported replay interval {raw_interval!r}") from exc
    # A synthetic intrabar path over a daily+ bucket has nothing to walk, so
    # only the registry's intraday rows may run a replay.
    if not TIMEFRAMES[timeframe.value].get("intraday"):
        raise ValueError(f"unsupported replay interval {raw_interval!r}")

    method = msg.get("method", "ohlc")
    if not isinstance(method, str) or method not in {"ohlc", "anchored", "bridge"}:
        raise ValueError("method must be ohlc, anchored, or bridge")

    paused = msg.get("paused", False)
    if not isinstance(paused, bool):
        raise ValueError("paused must be a boolean")

    return ReplayStartParams(
        instrument=instrument,
        timeframe=timeframe,
        method=method,
        seed=(
            _bounded(msg["seed"], "seed", 0, -(2**63), 2**63, as_int=True)
            if msg.get("seed") is not None
            else None
        ),
        speed=_bounded(msg.get("speed"), "speed", 1.0, 0.01, 1000.0, as_int=False),
        ticks_per_bar=_bounded(msg.get("ticks_per_bar"), "ticks_per_bar", 10, 2, 1000, as_int=True),
        minutes=_bounded(msg.get("minutes"), "minutes", 390, 1, 10070, as_int=True),
        start_time=(
            _bounded(msg["start_time"], "start_time", 0, 0, 10**12, as_int=True)
            if msg.get("start_time") is not None
            else None
        ),
        paused=paused,
    )


@dataclass
class _RunState:
    """Mutable state of the one active run."""

    run_id: str
    params: ReplayStartParams
    candles: tuple[Candle, ...]
    target_keys: tuple[int, ...]
    cursor: int = 0
    paused: bool = False
    step_requested: bool = False
    speed: float = 1.0
    aggregator: Any | None = None
    task: asyncio.Task[None] | None = None
    terminal: bool = False
    #: Set when the aggregator closed a bar belonging to this run, so a step
    #: knows whether its target bucket finished without re-scanning candles.
    step_closed: bool = False
    last_closed_bar_index: int = 0
    #: Monotonic count of closed bars handed to the transport. A step waits for
    #: this to advance past its starting value, so the ack is causally after
    #: the bar it stepped over without depending on any queue's ordering.
    bars_emitted: int = 0
    #: Bumped at the start of a step; ``_await_step_bar`` waits for
    #: ``bars_emitted`` to exceed it.
    step_bar_mark: int = 0


class ReplayController:
    """Owns one replay run's lifecycle, feeding a :class:`BarAggregator`.

    All transports are injected, so the lifecycle is exercisable with no
    WebSocket — which is the point: replay semantics should not need a socket
    to be tested.

    Parameters
    ----------
    make_aggregator :
        ``(instrument, timeframe_value, run_id, on_frame) -> aggregator``.
        Injected because the aggregator's ``on_frame`` callback is
        connection-scoped.
    emit_control :
        Sink for lifecycle frames (``replay_started``, ``replay_paused``,
        ``replay_resumed``, ``replay_stopped``, ``replay_error`` …). **Must be
        non-lossy** — the transport closes the socket rather than drop one.
    emit_bar :
        Sink for bar frames. Lossy by policy (market data is freshness-bound).
    emit_barrier :
        Optional sink for frames that assert something about bars already
        produced, and so must not overtake a bar still sitting in the
        transport's queue. Defaults to ``emit_control``, which is correct only
        when both share one FIFO — as they no longer do. The step ack uses
        this, because "the bar you stepped over is on the wire" has to be true
        on the wire, not just inside this process.
    load_candles :
        ``(params) -> Sequence[Candle]``. Injected so the datalake import and
        its fallback live behind one seam.
    on_bar_frame :
        Optional per-connection hook invoked after a bar is emitted (indicator
        registry, integrity tracker). Only called for bars this run closed.
    guard :
        Process-wide exclusivity holder; at most one run may hold it.
    purge_run_frames :
        Optional ``(run_id) -> None`` asking the transport to discard still-
        queued bars for a finished run, so a stopped replay cannot leave bars
        arriving after its terminal frame.
    """

    def __init__(
        self,
        *,
        make_aggregator: Callable[[str, str, str, Callable[[Any], None]], Any],
        emit_control: EmitFn,
        emit_bar: EmitFn,
        load_candles: Callable[[ReplayStartParams], Sequence[Candle]],
        on_bar_frame: Callable[[Any], None] | None = None,
        guard: Any | None = None,
        purge_run_frames: Callable[[str], None] | None = None,
        sleep: Callable[[float], Awaitable[None]] | None = None,
        emit_barrier: EmitFn | None = None,
        await_ticks_idle: Callable[[], Awaitable[None]] | None = None,
    ) -> None:
        self._make_aggregator = make_aggregator
        self._emit_control = emit_control
        self._emit_bar = emit_bar
        self._emit_barrier = emit_barrier or emit_control
        self._await_ticks_idle = await_ticks_idle
        self._load_candles = load_candles
        self._on_bar_frame = on_bar_frame
        self._guard = guard
        self._purge = purge_run_frames
        self._sleep = sleep or asyncio.sleep
        self._state = ReplayState.IDLE
        self._run: _RunState | None = None
        self._stop_ack_sent = False

    # -- introspection -------------------------------------------------

    @property
    def state(self) -> ReplayState:
        """Current lifecycle state — the single source of truth."""
        return self._state

    @property
    def run_id(self) -> str | None:
        return self._run.run_id if self._run is not None else None

    @property
    def instrument(self) -> str | None:
        return self._run.params.instrument if self._run is not None else None

    @property
    def cursor(self) -> int:
        return self._run.cursor if self._run is not None else 0

    @property
    def terminal(self) -> bool:
        return self._state in _TERMINAL_STATES

    @property
    def speed(self) -> float:
        return self._run.speed if self._run is not None else 1.0

    @property
    def active(self) -> bool:
        return self._state in _LIVE_STATES

    # -- transition validation ----------------------------------------

    def _next(self, command: str) -> ReplayState:
        """Resolve *command* against the current state, or refuse it."""
        try:
            return _TRANSITIONS[(self._state, command)]
        except KeyError:
            raise ReplayTransitionError(
                f"cannot {command} a replay that is {self._state.value}"
            ) from None

    def _error(self, message: str, run_id: str | None = None) -> None:
        frame: dict[str, Any] = {"type": "replay_error", "message": str(message)}
        if run_id is not None:
            frame["run_id"] = run_id
        self._emit_control(frame)

    # -- commands ------------------------------------------------------

    async def start(
        self,
        params: ReplayStartParams,
        *,
        dataset: Sequence[Candle] | None = None,
    ) -> str:
        """Start a replay, returning the new run id.

        Idempotent in the sense that matters: a second ``start`` replaces the
        first (the previous run is stopped and its guard slot released first),
        matching the one-active-replay-per-process rule.
        """
        # Validate before touching any state, so a refused start leaves the
        # existing run untouched rather than half-replaced.
        self._next("start") if self._state is ReplayState.IDLE else None

        try:
            candles = tuple(dataset) if dataset is not None else tuple(self._load_candles(params))
        except Exception as exc:
            self._error(str(exc))
            raise
        if not candles:
            self._error("replay dataset is empty")
            raise ValueError("replay dataset is empty")
        if any(getattr(c, "timeframe", None) is not Timeframe.M1 for c in candles):
            self._error("replay dataset must contain M1 candles")
            raise ValueError("replay dataset must contain M1 candles")

        try:
            target_keys = self._target_keys(candles, params.timeframe)
        except Exception as exc:
            self._error(str(exc))
            raise

        run = _RunState(
            run_id=secrets.token_hex(12),
            params=params,
            candles=candles,
            target_keys=target_keys,
            speed=params.speed,
            paused=False,
        )
        # Release the previous run *before* acquiring, so the guard is never
        # held by two ids at once even for an instant.
        if self._run is not None:
            await self._stop_current(silent=True)

        # The aggregator's on_frame is wired here rather than passed to the
        # factory so the per-connection "is this bar ours / did our bucket
        # close" bookkeeping stays with the run that owns it.
        def on_frame(frame: Any) -> None:
            self._bar_frame(run, frame)

        try:
            run.aggregator = self._make_aggregator(
                run.params.instrument, run.params.interval, run.run_id, on_frame
            )
        except Exception as exc:
            self._error(f"cannot start replay bars: {exc}")
            raise

        if self._guard is not None:
            try:
                self._guard.acquire(run.run_id)
            except Exception as exc:
                self._dispose_aggregator(run)
                self._error(f"cannot start replay: {exc}")
                raise

        self._run = run
        self._stop_ack_sent = False
        self._state = ReplayState.CREATED

        run.task = asyncio.create_task(self._feed_loop(run))
        run.task.add_done_callback(lambda t: self._on_task_done(run, t))
        self._state = self._next("run")

        if params.paused:
            # "Start paused" means: the run exists and is armed, but the feed
            # holds before its first candle until the client resumes or steps.
            # The task still runs (it is what will produce those bars), but the
            # lifecycle state is PAUSED, so a step/resume is legal and a second
            # pause is correctly refused.
            run.paused = True
            self._state = ReplayState.PAUSED

        self._emit_control(
            {
                "type": "replay_started",
                "run_id": run.run_id,
                "instrument": run.params.instrument,
                "interval": run.params.interval,
                "start_time": target_keys[0] if target_keys else None,
                "total_bars": len(target_keys),
                "simulated": True,
                "wall_seconds_per_bar": 1.0 / max(run.speed, 0.01),
            }
        )
        return run.run_id

    async def pause(self) -> None:
        """Pause a running replay. Refuses from any other state."""
        self._state = self._next("pause")
        if self._run is not None:
            self._run.paused = True
        self._emit_control({"type": "replay_paused", "run_id": self.run_id})

    async def resume(self) -> None:
        """Resume a paused replay. Refuses from any other state."""
        self._state = self._next("resume")
        if self._run is not None:
            self._run.paused = False
        self._emit_control({"type": "replay_resumed", "run_id": self.run_id})

    async def step(self) -> None:
        """Advance exactly one target bar, then re-pause.

        Does not emit its own ack here: the feed loop emits ``replay_stepped``
        only *after* the bar it advanced through has been published, which is
        the causal-ordering guarantee the plan asks for. An ack emitted here
        would let the client advance its view before the bar arrived.
        """
        self._state = self._next("step")
        if self._run is None:
            return
        self._run.step_requested = True
        self._run.step_closed = False

    def set_speed(self, raw: Any) -> float:
        """Change replay speed. Valid from any live state."""
        if self._run is None or not self.active:
            raise ReplayTransitionError("cannot set speed on an inactive replay")
        try:
            speed = _bounded(raw, "speed", 1.0, 0.01, 1000.0, as_int=False)
        except ValueError as exc:
            self._error(str(exc), self.run_id)
            raise
        self._run.speed = speed
        self._emit_control({"type": "replay_speed", "speed": speed, "run_id": self.run_id})
        return speed

    async def seek(self, msg: dict[str, Any]) -> None:
        """Restart the run at a target bar, keeping the same dataset.

        Refuses from IDLE — seeking implies a run to seek within — and from
        nothing else.
        """
        self._next("seek")
        run = self._run
        if run is None:
            self._error("replay_seek needs an active replay")
            return
        try:
            if msg.get("index") is not None or msg.get("target_index") is not None:
                raw_index = msg.get("index", msg.get("target_index"))
                index = _bounded(
                    raw_index, "replay_seek index", 0, 0, len(run.target_keys) - 1, as_int=True
                )
                start_time = run.target_keys[index]
            else:
                start_time = _bounded(
                    msg.get("start_time"), "replay_seek start_time", 0, 0, 10**12, as_int=True
                )
            candle_index = self._index_for_key(run, start_time)
            if candle_index >= len(run.candles):
                raise KeyError(start_time)
        except (KeyError, ValueError) as exc:
            self._error(str(exc), run.run_id)
            await self.stop()
            return

        dataset = run.candles[candle_index:]
        params = run.params
        previous_run_id = run.run_id
        await self._stop_current(silent=True)
        try:
            await self.start(params, dataset=dataset)
        except Exception:
            self._error("replay seek restart failed", previous_run_id)
            raise
        if self.run_id is None:
            self._error("replay seek restart failed", previous_run_id)

    async def stop(self) -> None:
        """Stop any run. From IDLE this is a no-op ack, not a transition."""
        if self._state is ReplayState.IDLE:
            if not self._stop_ack_sent:
                self._emit_control({"type": "replay_stopped"})
                self._stop_ack_sent = True
            return
        self._next("stop")
        await self._stop_current(silent=False)

    async def close(self) -> None:
        """Tear everything down without emitting (connection is going away)."""
        await self._stop_current(silent=True)
        self._state = ReplayState.IDLE

    # -- internals -----------------------------------------------------

    @staticmethod
    def _target_keys(candles: Sequence[Candle], timeframe: Timeframe) -> tuple[int, ...]:
        """One bar key per distinct target-timeframe bucket, in order."""
        from tradex_runtime.bar_aggregator import bucket_start

        seconds = interval_seconds(timeframe)
        keys: list[int] = []
        for candle in candles:
            key = candle_epoch(bucket_start(candle.timestamp, seconds))
            if not keys or key != keys[-1]:
                keys.append(key)
        return tuple(keys)

    @staticmethod
    def _index_for_key(run: _RunState, key: int) -> int:
        needle = int(key)
        for index, candle in enumerate(run.candles):
            if candle.timestamp is not None:
                seconds = interval_seconds(run.params.timeframe)
                epoch = candle_epoch(candle.timestamp)
                if epoch - (epoch % seconds) == needle:
                    return index
        try:
            return run.target_keys.index(needle)
        except ValueError as exc:
            raise KeyError(needle) from exc

    def _bar_frame(self, run: _RunState, frame: Any) -> None:
        """Publish one bar frame from this run's aggregator.

        Closed bars belonging to this run also update the step bookkeeping, so
        a step knows whether its target bucket actually closed instead of
        re-scanning the dataset. Bars from a finished run are dropped: they are
        stale by definition and would arrive after the terminal frame.
        """
        if (
            getattr(frame, "closed", False) is True
            and getattr(frame, "run_id", None) == run.run_id
            and frame.instrument == run.params.instrument
            and frame.timeframe == run.params.interval
        ):
            run.step_closed = True
            try:
                run.last_closed_bar_index = run.target_keys.index(int(frame.time))
            except ValueError:
                pass
        self._emit_bar(            {
                "type": "bar",
                "instrument": frame.instrument,
                "interval": frame.timeframe,
                "time": frame.time,
                "open": frame.open,
                "high": frame.high,
                "low": frame.low,
                "close": frame.close,
                "volume": frame.volume,
                "closed": frame.closed,
                "source": getattr(frame, "source", "live"),
                "run_id": getattr(frame, "run_id", None),
                "provisional": bool(getattr(frame, "provisional", False)),
            }
        )
        if self._on_bar_frame is not None:
            self._on_bar_frame(frame)
        if getattr(frame, "closed", False) is True:
            run.bars_emitted += 1

    async def _await_step_bar(self, run: _RunState) -> None:
        """Wait until this step's closed bar has reached the wire.

        Two waits, both required:

        1. The controller has emitted a closed bar (``bars_emitted`` advanced).
        2. The transport's tick queue is idle — otherwise ``_finish``'s purge
           of queued sim bars (needed so a STOPPED frame is not followed by
           stale bars) would discard the bar this step just produced before
           the writer sent it. Observed as ``replay_stepped`` with no
           preceding closed bar on the wire.

        Returns early if the run terminates. The tick bound is a safety valve.
        """
        if run.bars_emitted <= run.step_bar_mark and not run.terminal:
            for _ in range(_STEP_BAR_WAIT_TICKS):
                if run.bars_emitted > run.step_bar_mark or run.terminal:
                    break
                await self._sleep(0.01)
            else:
                log.warning(
                    "replay %s: step ack with no closed bar emitted "
                    "(mark=%d, emitted=%d)",
                    run.run_id, run.step_bar_mark, run.bars_emitted,
                )
        if self._await_ticks_idle is not None and not run.terminal:
            await self._await_ticks_idle()

    async def _stop_current(self, *, silent: bool) -> None:
        run = self._run
        if run is None:
            if not silent and not self._stop_ack_sent:
                self._emit_control({"type": "replay_stopped"})
                self._stop_ack_sent = True
            return
        error: str | None = None
        try:
            error = await _cancel_and_drain(run)
        finally:
            if self._run is run:
                self._run = None
            if self._guard is not None:
                try:
                    self._guard.release(run.run_id)
                except Exception:
                    log.exception("replay guard release failed for %s", run.run_id)
            if self._purge is not None:
                try:
                    self._purge(run.run_id)
                except Exception:
                    log.exception("replay frame purge failed for %s", run.run_id)
            self._state = ReplayState.STOPPED
            if not silent:
                frame: dict[str, Any] = {"type": "replay_stopped", "run_id": run.run_id}
                if error:
                    frame["message"] = error
                self._emit_control(frame)
                self._stop_ack_sent = True

    async def _finish(self, run: _RunState, terminal_type: str, message: str | None = None) -> None:
        if run.terminal or self._run is not run:
            return
        run.terminal = True
        run.paused = False
        run.step_requested = False
        self._dispose_aggregator(run)
        if self._run is run:
            self._run = None
        if self._guard is not None:
            try:
                self._guard.release(run.run_id)
            except Exception:
                log.exception("replay guard release failed for %s", run.run_id)
        if self._purge is not None and terminal_type != "replay_done":
            # Purge only on stop/error: a clean ``replay_done`` must leave the
            # bars it just produced on the wire. Purging here was observed to
            # discard the closed bar of a last-candle step before the writer
            # could send it, so the client got ``replay_stepped`` with no bar.
            try:
                self._purge(run.run_id)
            except Exception:
                log.exception("replay frame purge failed for %s", run.run_id)
        # Wait for bars (and any step barrier that just preceded us) to leave
        # the transport before the terminal frame. ``replay_done`` is control-
        # priority; without this wait it overtakes a ``replay_stepped`` still
        # sitting on the barrier lane.
        if self._await_ticks_idle is not None:
            await self._await_ticks_idle()
        self._state = self._TERMINAL.get(terminal_type, ReplayState.STOPPED)
        frame: dict[str, Any] = {"type": terminal_type, "run_id": run.run_id}
        if message is not None:
            frame["message"] = message
        self._emit_control(frame)

    #: terminal frame type → lifecycle state
    _TERMINAL = {
        "replay_done": ReplayState.DONE,
        "replay_stopped": ReplayState.STOPPED,
        "replay_error": ReplayState.ERROR,
    }

    def _dispose_aggregator(self, run: _RunState) -> None:
        aggregator = run.aggregator
        if aggregator is not None:
            try:
                aggregator.flush()
            finally:
                try:
                    aggregator.dispose()
                except Exception:
                    log.exception("aggregator dispose failed for %s", run.run_id)
            run.aggregator = None

    def _on_task_done(self, run: _RunState, task: asyncio.Task[None]) -> None:
        if task.cancelled():
            return
        try:
            error = task.exception()
        except asyncio.CancelledError:
            return
        if error is not None and self._run is run and not run.terminal:
            log.exception("replay task failed for %s", run.run_id, exc_info=error)
            asyncio.create_task(self._finish(run, "replay_error", str(error)))

    async def _feed_loop(self, run: _RunState) -> None:
        from tradex_reactive.bus import ReactiveBus
        from tradex_replay.synthetic_ticks import SyntheticTickGenerator

        params = run.params
        try:
            generator = SyntheticTickGenerator(
                ReactiveBus(),
                ticks_per_bar=params.ticks_per_bar,
                seed=params.seed,
                method=params.method,
            )
        except Exception as exc:
            await self._finish(run, "replay_error", str(exc))
            return

        async def _feed(candle: Candle, *, wait: bool) -> None:
            quotes = generator.iter_ticks(candle)
            n_prints = max(len(quotes), 1)
            delay = (1.0 / max(run.speed, 0.01)) / n_prints if wait else 0.0
            for quote in quotes:
                # Pause only while playing. A step clears step_requested before
                # feeding and must not block on paused=True.
                if wait:
                    while run.paused and not run.step_requested and not run.terminal:
                        await self._sleep(0.01)
                if run.terminal:
                    return
                ts = quote.timestamp
                if ts is None:
                    await self._finish(run, "replay_error", "replay quote has no timestamp")
                    return
                if ts.tzinfo is not None:
                    ts = ts.astimezone(IST_ZONE).replace(tzinfo=None)
                # Aggregator only — never the session bus, quote frames, or
                # last_ltp: a replay is not live market data.
                if run.aggregator is not None:
                    run.aggregator.on_quote(
                        ts,
                        quote.ltp.value,
                        quote.volume.value if quote.volume is not None else None,
                        source="sim",
                    )
                if wait and delay > 0.001:
                    await self._sleep(delay)

        try:
            while run.cursor < len(run.candles) and not run.terminal:
                # A paused run holds *before* its next candle. That is also what
                # makes "start paused" mean something: the run is armed and the
                # client decides when the first bar is produced (by resuming or
                # stepping), rather than the server racing one out immediately.
                while run.paused and not run.step_requested and not run.terminal:
                    await self._sleep(0.01)
                if run.terminal:
                    return
                stepping = run.step_requested
                if stepping:
                    run.step_requested = False
                    run.step_closed = False
                    run.step_bar_mark = run.bars_emitted
                candle = run.candles[run.cursor]
                run.cursor += 1
                await _feed(candle, wait=not stepping)
                if stepping:
                    if params.timeframe is Timeframe.M1:
                        # One M1 candle is one target bar — flush to close
                        # without burning the next minute.
                        if not run.step_closed and run.aggregator is not None:
                            run.aggregator.flush()
                    else:
                        # Higher TF: keep feeding M1s until the target bucket
                        # closes, then flush if needed.
                        while (
                            not run.step_closed
                            and run.cursor < len(run.candles)
                            and not run.terminal
                        ):
                            await _feed(run.candles[run.cursor], wait=False)
                            run.cursor += 1
                        if not run.step_closed and run.aggregator is not None:
                            run.aggregator.flush()
                    if not run.terminal:
                        run.paused = True
                        # Ordering contract: the step ack must not be published
                        # until the bar it stepped over has been handed to the
                        # transport. An ``await asyncio.sleep(0)`` used to make
                        # this work by accident — the bar and the ack shared one
                        # FIFO queue, so enqueue order was wire order. They no
                        # longer share a queue (bars are lossy, control is not),
                        # so the ordering is stated here instead of inherited
                        # from a queue's properties: wait for the run's last
                        # emitted bar to be the one this step closed.
                        await self._await_step_bar(run)
                        self._state = ReplayState.PAUSED
                        # Ticks are idle (or we timed out waiting). Emit the
                        # step ack on the control lane so a subsequent
                        # ``replay_done`` — also control — cannot overtake it.
                        # Emitting on the barrier lane here would lose: the
                        # writer drains control before barrier, so done would
                        # land first.
                        self._emit_control(
                            {
                                "type": "replay_stepped",
                                "run_id": run.run_id,
                                "bar_index": run.last_closed_bar_index,
                            }
                        )
                        self._emit_control({"type": "replay_paused", "run_id": run.run_id})
            if not run.terminal:
                if run.aggregator is not None:
                    run.aggregator.flush()
                await self._finish(run, "replay_done")
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            log.exception("replay task failed for %s", run.run_id)
            await self._finish(run, "replay_error", str(exc))


async def _cancel_and_drain(run: _RunState) -> str | None:
    """Cancel the feed task and dispose its aggregator; return any error."""
    task = run.task
    run.task = None
    error: str | None = None
    current = asyncio.current_task()
    if task is not None and task is not current:
        if not task.done():
            task.cancel()
        try:
            await task
        except asyncio.CancelledError:
            pass
        except Exception as exc:
            error = str(exc)
    # Mirror ReplayRun.close(): flush the partial bar, then dispose.
    run.terminal = True
    run.paused = False
    run.step_requested = False
    aggregator = run.aggregator
    if aggregator is not None:
        try:
            aggregator.flush()
        except Exception as exc:
            error = error or str(exc)
        finally:
            try:
                aggregator.dispose()
            except Exception as exc:
                error = error or str(exc)
        run.aggregator = None
    return error


__all__ = [
    "ReplayController",
    "ReplayState",
    "ReplayStartParams",
    "ReplayTransitionError",
    "parse_replay_start",
]
