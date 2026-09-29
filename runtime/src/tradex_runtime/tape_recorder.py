"""Tick tape recorder: capture what a venue actually sent, read it back exactly.

The durable log in this platform is order events, so nothing in the repository
could record a *market data session*. Two accepted requirements need one: the
comparison of the two paper fill paths (AC-14, lane 2d) and the backtest/live
parity suite (lane 1l). Both drive fills from a recorded session, so both are
unwriteable without it.

**The gate is the point.** ``TRADEX_RECORD_TAPE=1`` enables recording; every
other value, and an unset variable, means the recorder does not exist and
nothing is created on disk. A tick tape is large and the datalake beside it is
already large, so a recorder that quietly writes because nobody thought about
the default is a disk filler. The check is the first thing
:func:`open_tape_recorder` and :class:`TapeRecorder` both do, so there is no
path to the file that does not pass through it — constructing the class
directly with the gate shut raises rather than opening a handle.

**Durability.** Each line is written and pushed out of the process buffer
immediately, because the stated purpose is surviving a process killed mid
session, and a kill never runs ``close``. ``flush`` and ``rotate`` are explicit
and observable for that reason; they are checkpoints, not the thing that keeps
the tape on disk.

**Fidelity.** :func:`read_tape` returns the file order verbatim: no sorting, no
de-duplication, no gap filling. The consumers assert on fills, so a reader that
dropped a duplicate or reordered a timestamp would make those assertions
meaningless rather than merely imprecise. A line the reader cannot parse raises
:class:`TapeReadError` naming the line rather than being skipped, for the same
reason.

Record format, one JSON object per line::

    {"broker": "dhan", "symbol": "NSE:RELIANCE",
     "ts": "2026-09-24T10:15:30.123456+05:30",
     "price": "2500.50", "qty": "1000"}

* ``broker`` is the venue the quote came from (``Quote.provider``), falling back
  to the recorder's label when the quote carries none.
* ``symbol`` is the platform key, ``str(InstrumentId)``.
* ``ts`` is IST carrying its offset — the platform's storage convention
  (CLAUDE.md), written with the offset so the instant is unambiguous on the way
  back. A naive value in the file is read as IST wall clock, which is what the
  file's own convention means; reading it as UTC would move every tick by
  5h30m.
* ``price`` and ``qty`` are decimal *strings*, not JSON numbers. A JSON number
  is a float, and the two requirements that consume this assert on fill prices.
  ``qty`` is ``null`` when the quote carried no volume: writing ``0`` would
  invent data the venue never sent, and a zero volume reads as "traded flat".
* Unknown keys are preserved on :attr:`RecordedTick.extra` and ignored by the
  recorder. This is the first slice of a market data tape rather than a closed
  format, so the next field (``bid``/``ask``/depth) has to be addable without a
  reader rewrite.

Files land in ``<runtime dir>/tapes`` unless a directory is passed, named
``tape-<broker>-<IST date>.jsonl`` and rotated to ``.0001``, ``.0002``, … past
``max_bytes``. The date is the construction date, so a restart on the same day
appends to the same file (a kill mid session must not destroy what was already
captured) and a new day starts a new one. Append, never truncate.

Deliberately absent, because the first version of this is the prerequisite for
two fill assertions and not a tape store: depth/book snapshots, a header or
manifest, a schema version, session identifiers, replay into a bus, and any
index. See the lane report for what a later wave has to add on top.
"""

from __future__ import annotations

import json
import os
import re
import threading
from collections.abc import Callable, Mapping
from dataclasses import dataclass, field
from datetime import UTC, datetime
from decimal import Decimal, InvalidOperation
from pathlib import Path
from types import MappingProxyType
from typing import TextIO

from tradex_brokers.common.instruments import instrument_from_id
from tradex_domain.constants import IST_TIMEZONE
from tradex_domain.instruments import Instrument
from tradex_domain.market import Quote
from tradex_domain.paths import default_runtime_dir
from tradex_domain.value_objects import InstrumentId, Price, Quantity

#: Environment variable that enables recording. The only accepted value is
#: ``"1"``: ``bool(os.environ.get(...))`` reads ``"0"`` and ``"false"`` as on,
#: which is how a gated feature ends up writing in production.
TAPE_RECORD_ENV_VAR = "TRADEX_RECORD_TAPE"

#: The one value of :data:`TAPE_RECORD_ENV_VAR` that turns recording on.
TAPE_RECORD_ON = "1"

#: Sub-directory of the runtime root that tapes are written to.
TAPE_DIR_NAME = "tapes"

#: Rotate once the current segment reaches this many bytes.
DEFAULT_MAX_BYTES = 64 * 1024 * 1024

#: The record's own keys. Anything else in a line is a later wave's field and
#: rides along in ``extra``.
RECORD_KEYS = frozenset({"broker", "symbol", "ts", "price", "qty"})

#: IST, the platform's storage convention (CLAUDE.md).
IST = IST_TIMEZONE

_UNSAFE_NAME = re.compile(r"[^A-Za-z0-9._-]+")
_SEGMENT = re.compile(r"\.(\d+)\.jsonl$")


class TapeRecordingDisabled(RuntimeError):
    """The tape was opened for writing while recording is not enabled.

    Raised rather than returned so that a direct
    :class:`TapeRecorder` construction cannot bypass the gate that
    :func:`open_tape_recorder` enforces on its own return value.
    """


class TapeReadError(RuntimeError):
    """A tape line could not be read back as a tick.

    The tape is the only record of the session, so an unreadable line is a hole
    in the sequence a fill assertion would be made against. The read fails
    loudly instead of dropping the line.
    """


def tape_recording_enabled() -> bool:
    """Whether tick recording is switched on by the environment."""
    return os.environ.get(TAPE_RECORD_ENV_VAR) == TAPE_RECORD_ON


def default_tape_dir() -> Path:
    """``<runtime dir>/tapes``, resolved without creating anything.

    ``create=False`` because the caller's negative control is that an
    unenabled recorder leaves the filesystem untouched, and creating the
    runtime root on the way to deciding that would break it.
    """
    return default_runtime_dir(create=False) / TAPE_DIR_NAME


def _slug(label: str) -> str:
    """A filename-safe form of *label*.

    The label reaches the path, so it must not be able to choose where the file
    lands. An empty or dot-only result falls back to ``unknown`` so the stem is
    never ``tape-.jsonl`` or ``tape-..jsonl``.
    """
    cleaned = _UNSAFE_NAME.sub("-", str(label or "").strip()).strip("-")
    if cleaned in {"", ".", ".."}:
        return "unknown"
    return cleaned


def _as_ist(ts: datetime) -> datetime:
    """A quote timestamp as an aware IST datetime.

    Brokers that omit an offset report UTC (``live_fill_bridge`` reads them the
    same way), so a naive tick is UTC before it is IST.
    """
    if ts.tzinfo is None:
        ts = ts.replace(tzinfo=UTC)
    return ts.astimezone(IST)


def _as_decimal(value: object) -> Decimal:
    """Coerce a tape number, refusing anything that is not a finite decimal."""
    if value is None or isinstance(value, bool):
        raise ValueError(f"not a decimal: {value!r}")
    try:
        number = Decimal(str(value))
    except (ArithmeticError, InvalidOperation, TypeError, ValueError) as exc:
        raise ValueError(f"not a decimal: {value!r}") from exc
    if not number.is_finite():
        raise ValueError(f"not a finite decimal: {value!r}")
    return number


@dataclass(frozen=True, slots=True)
class RecordedTick:
    """One quote as it was recorded, in the order it was recorded.

    Comparable but not hashable, because ``extra`` is a mapping.
    """

    broker: str
    symbol: str
    ts: datetime
    price: Decimal
    qty: Decimal | None = None
    extra: Mapping[str, object] = field(default_factory=dict)

    def __post_init__(self) -> None:
        object.__setattr__(self, "extra", MappingProxyType(dict(self.extra)))

    @property
    def ts_ist(self) -> datetime:
        """The tick's time as tz-naive IST.

        The shape ``BarAggregator.on_quote`` and the datalake both take, so a
        replay can aggregate these into bars without anyone re-deriving the
        zone — and without a naive value being read as UTC somewhere.
        """
        return self.ts.astimezone(IST).replace(tzinfo=None)

    def to_quote(
        self, resolver: Callable[[InstrumentId], Instrument] | None = None
    ) -> Quote:
        """This tick as a domain ``Quote``, resolved by instrument key.

        ``resolver`` takes the parsed :class:`InstrumentId`; it defaults to the
        platform's own ``instrument_from_id``. It exists because a tape stores a
        key, and the key has to become a security the same way it did when the
        quote was recorded.
        """
        instrument = (resolver or instrument_from_id)(InstrumentId.parse(self.symbol))
        return Quote(
            instrument=instrument,
            ltp=Price(value=self.price),
            volume=None if self.qty is None else Quantity(value=self.qty),
            timestamp=self.ts,
            exchange=instrument.exchange.value,
            provider=self.broker,
        )


class TapeRecorder:
    """Append JSONL tick records to one tape segment, rotating on size.

    Constructing this with :data:`TAPE_RECORD_ENV_VAR` unset raises
    :class:`TapeRecordingDisabled` and creates nothing, so the gate cannot be
    walked around by instantiating the class rather than going through
    :func:`open_tape_recorder`.
    """

    def __init__(
        self,
        path: str | Path,
        *,
        broker: str = "unknown",
        max_bytes: int = DEFAULT_MAX_BYTES,
    ) -> None:
        """
        Parameters
        ----------
        path:
            The segment to append to. Created if missing; never truncated, so a
            process that restarts mid session keeps what it had already
            recorded.
        broker:
            Fallback venue label for quotes that carry no provider.
        max_bytes:
            Rotate once the current segment reaches this size.
        """
        if not tape_recording_enabled():
            raise TapeRecordingDisabled(
                f"tick recording is off; set {TAPE_RECORD_ENV_VAR}={TAPE_RECORD_ON} "
                "to record a tape"
            )
        if max_bytes <= 0:
            raise ValueError("max_bytes must be positive")
        self._base = Path(path).expanduser().resolve()
        self._path = self._base
        self._stem = self._base.with_suffix("")
        self._broker = _slug(broker)
        self._max_bytes = int(max_bytes)
        self._segment = 0
        self._bytes = 0
        self._lock = threading.Lock()
        self._handle: TextIO | None = None
        self._path.parent.mkdir(parents=True, exist_ok=True)
        self._handle = self._open(self._path)

    @property
    def path(self) -> Path:
        """The segment currently being written. Moves on rotation."""
        return self._path

    @property
    def base_path(self) -> Path:
        """The first segment of this series, which rotation does not move.

        This is what :func:`read_tape_stem` takes: a series is named by the file
        it started in, and the numbered segments are derived from it.
        """
        return self._base

    @property
    def broker(self) -> str:
        """Fallback venue label for quotes that carry no provider."""
        return self._broker

    @property
    def recorded(self) -> int:
        """Bytes written to the current segment."""
        return self._bytes

    def _open(self, path: Path) -> TextIO:
        # Line buffered, so every record reaches the OS as it is written: a
        # killed process never runs close, and the tape is the one artifact
        # that has to survive that. newline="" is a record file, so nothing
        # translates the terminator.
        return path.open("a", encoding="utf-8", buffering=1, newline="\n")

    def _line(self, quote: Quote) -> str:
        return (
            json.dumps(
                {
                    "broker": str(quote.provider or self._broker),
                    "symbol": str(quote.instrument.instrument_id),
                    "ts": _as_ist(quote.timestamp).isoformat(),
                    "price": str(quote.ltp.value),
                    "qty": None if quote.volume is None else str(quote.volume.value),
                },
                separators=(",", ":"),
            )
            + "\n"
        )

    def record(self, quote: Quote) -> None:
        """Append one quote to the tape.

        Raises ``RuntimeError`` once the recorder is closed, rather than
        dropping the tick: a silent no-op here would leave a gap in the
        evidence with nothing to say so.
        """
        with self._lock:
            if self._handle is None:
                raise RuntimeError("tape recorder is closed")
            line = self._line(quote)
            self._handle.write(line)
            self._bytes += len(line.encode("utf-8"))
            if self._bytes >= self._max_bytes:
                self._rotate_locked()

    def flush(self) -> None:
        """Push buffered records out. A no-op once closed."""
        with self._lock:
            if self._handle is not None:
                self._handle.flush()

    def rotate(self) -> Path | None:
        """Flush, close and continue into the next numbered segment.

        Returns the new segment, or ``None`` when the current one is empty —
        rotating nothing would leave a blank file in the series for a reader to
        step over.
        """
        with self._lock:
            return self._rotate_locked(force=False)

    def _rotate_locked(self, *, force: bool = True) -> Path | None:
        if self._handle is None:
            return None
        self._handle.flush()
        if self._bytes == 0 and not force:
            return None
        self._handle.close()
        self._segment += 1
        self._bytes = 0
        self._path = self._stem.with_name(
            f"{self._stem.name}.{self._segment:04d}.jsonl"
        )
        self._handle = self._open(self._path)
        return self._path

    def close(self) -> None:
        """Flush and close. Idempotent — shutdown paths may close twice."""
        with self._lock:
            if self._handle is None:
                return
            self._handle.flush()
            self._handle.close()
            self._handle = None

    def __enter__(self) -> TapeRecorder:
        return self

    def __exit__(self, *_exc: object) -> None:
        self.close()


def open_tape_recorder(
    *,
    broker: str = "unknown",
    directory: str | Path | None = None,
    max_bytes: int = DEFAULT_MAX_BYTES,
    session: str | None = None,
) -> TapeRecorder | None:
    """Open a recorder for this session, or ``None`` when recording is off.

    ``None`` is the signal, not an exception: a caller wiring this into a feed
    wants to say ``if recorder is not None: recorder.record(quote)`` and get a
    ``TypeError`` if it forgot, not a silent write to the default location.

    The environment check comes first, before any path is resolved, so an
    unenabled caller creates no directory and no file.
    """
    if not tape_recording_enabled():
        return None
    root = Path(directory) if directory is not None else default_tape_dir()
    label = session if session is not None else datetime.now(IST).strftime("%Y%m%d")
    path = root / f"tape-{_slug(broker)}-{_slug(label)}.jsonl"
    return TapeRecorder(path, broker=broker, max_bytes=max_bytes)


def _parse_line(line: str, number: int, *, last: bool) -> RecordedTick:
    try:
        row = json.loads(line)
    except ValueError as exc:
        hint = (
            "; the file does not end in a newline, so this is most likely a "
            "truncated tail from a process killed mid write"
            if last
            else ""
        )
        raise TapeReadError(f"tape line {number} is not valid JSON{hint}") from exc
    if not isinstance(row, dict):
        raise TapeReadError(f"tape line {number} is not a JSON object")

    missing = sorted(RECORD_KEYS - row.keys())
    if missing:
        raise TapeReadError(
            f"tape line {number} is missing required field(s): {', '.join(missing)}"
        )
    if not isinstance(row["ts"], str):
        raise TapeReadError(f"tape line {number}: ts is not a timestamp: {row['ts']!r}")
    try:
        ts = datetime.fromisoformat(row["ts"])
    except ValueError as exc:
        raise TapeReadError(f"tape line {number}: ts: {exc}") from exc
    for name in ("price", "qty"):
        if name == "qty" and row["qty"] is None:
            continue
        try:
            _as_decimal(row[name])
        except ValueError as exc:
            raise TapeReadError(f"tape line {number}: {name}: {exc}") from exc
    # Normalized to the module's IST rather than the fixed offset fromisoformat
    # hands back, so every tick's tzinfo is the same object the writer used.
    ts = ts.replace(tzinfo=IST) if ts.tzinfo is None else ts.astimezone(IST)
    return RecordedTick(
        broker=str(row["broker"]),
        symbol=str(row["symbol"]),
        ts=ts,
        price=_as_decimal(row["price"]),
        qty=None if row["qty"] is None else _as_decimal(row["qty"]),
        extra={k: v for k, v in row.items() if k not in RECORD_KEYS},
    )


def _split(content: str) -> list[str]:
    lines = content.split("\n")
    if lines and lines[-1] == "":
        lines.pop()
    return lines


def read_tape(path: str | Path) -> tuple[RecordedTick, ...]:
    """Every tick in *path*, in the order it was written.

    No sorting, no de-duplication, no gap detection: the file order is the
    session, including any out-of-order or repeated timestamp in it, because the
    assertions built on this compare fills.

    Raises
    ------
    TapeReadError
        On any line that is not a readable record, naming the line number.
        Blank lines are not records and are skipped.
    """
    content = Path(path).read_text(encoding="utf-8")
    lines = _split(content)
    last = len(lines)
    return tuple(
        _parse_line(line, number, last=number == last)
        for number, line in enumerate(lines, start=1)
        if line.strip()
    )


def read_tape_stem(base_path: str | Path) -> tuple[RecordedTick, ...]:
    """Every tick in a recorder's whole series: the base segment, then the
    numbered ones in order.

    Takes the first segment's path (:attr:`TapeRecorder.base_path`), which is
    how a session that rotated is read back as one ordered sequence. The base
    is read first because it holds the earliest ticks, and the segments are
    ordered numerically rather than lexicographically so the order still holds
    past 9999 rotations.
    """
    base = Path(base_path)
    series: list[Path] = [base] if base.is_file() else []
    segments = []
    for candidate in base.with_suffix("").parent.glob(f"{base.stem}.*.jsonl"):
        match = _SEGMENT.search(candidate.name)
        if match is not None:
            segments.append((int(match.group(1)), candidate))
    series.extend(path for _, path in sorted(segments, key=lambda item: item[0]))
    return tuple(tick for path in series for tick in read_tape(path))


__all__ = [
    "DEFAULT_MAX_BYTES",
    "IST",
    "RECORD_KEYS",
    "TAPE_DIR_NAME",
    "TAPE_RECORD_ENV_VAR",
    "TAPE_RECORD_ON",
    "RecordedTick",
    "TapeReadError",
    "TapeRecorder",
    "TapeRecordingDisabled",
    "default_tape_dir",
    "open_tape_recorder",
    "read_tape",
    "read_tape_stem",
    "tape_recording_enabled",
]
