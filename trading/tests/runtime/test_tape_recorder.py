"""Tests for the tick tape recorder (lane 2g, spec AC-14).

Two properties carry the weight, and each has a negative control:

1. **Off unless asked for.** With ``TRADEX_RECORD_TAPE`` unset, nothing is
   created on disk. The control that makes this a real assertion rather than a
   vacuous one is the paired *enabled* test asserting the same directory IS
   created — without it, a typo in the path resolution would make the "no file"
   test pass forever.
2. **The reader is lossless.** ``read_tape`` reproduces the recorded sequence
   exactly, including out-of-order and duplicate timestamps, and refuses a
   malformed line instead of skipping it. A reader that drops or reorders a tick
   does not merely lose precision: the two requirements that consume this
   (AC-14's two-fill-path comparison, lane 1l's parity suite) assert on fills,
   so a hole in the sequence is an assertion that cannot fail.
"""

from __future__ import annotations

import json
import threading
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from pathlib import Path
from zoneinfo import ZoneInfo

import pytest
from tradex_domain.instruments import Equity
from tradex_domain.market import Quote
from tradex_domain.value_objects import Price, Quantity
from tradex_runtime.tape_recorder import (
    TAPE_RECORD_ENV_VAR,
    TapeReadError,
    TapeRecorder,
    TapeRecordingDisabled,
    open_tape_recorder,
    read_tape,
    read_tape_stem,
    tape_recording_enabled,
)

IST = ZoneInfo("Asia/Kolkata")
INSTRUMENT = Equity.of("NSE", "RELIANCE")


def _quote(
    ts: datetime,
    ltp: str = "2500.50",
    *,
    volume: str | None = "1000",
    provider: str = "dhan",
) -> Quote:
    return Quote(
        instrument=INSTRUMENT,
        ltp=Price(value=Decimal(ltp)),
        volume=None if volume is None else Quantity(value=Decimal(volume)),
        timestamp=ts,
        exchange="NSE",
        provider=provider,
    )


def _lines(path: Path) -> list[dict]:
    return [json.loads(line) for line in path.read_text().splitlines() if line]


@pytest.fixture(autouse=True)
def _no_recording(monkeypatch: pytest.MonkeyPatch) -> None:
    """Every test starts with the gate shut; enabled tests open it explicitly."""
    monkeypatch.delenv(TAPE_RECORD_ENV_VAR, raising=False)


# -- the gate -----------------------------------------------------------------


class TestGate:
    def test_unset_env_creates_no_file(self, tmp_path: Path) -> None:
        """The primary control: unset means the recorder does not exist and
        nothing is created — not the file, not the directory."""
        target = tmp_path / "tapes"
        recorder = open_tape_recorder(broker="dhan", directory=target)

        assert recorder is None
        assert not target.exists()
        assert list(tmp_path.iterdir()) == []

    def test_enabled_env_does_create_the_file(self, tmp_path: Path, monkeypatch) -> None:
        """The negative control for the test above: the same call with the gate
        open really does create the file, so 'no file' is the gate and not a
        broken path."""
        monkeypatch.setenv(TAPE_RECORD_ENV_VAR, "1")
        target = tmp_path / "tapes"

        with open_tape_recorder(broker="dhan", directory=target) as recorder:
            assert recorder is not None
            assert recorder.path.exists()
            assert recorder.path.parent == target

    @pytest.mark.parametrize(
        "value",
        ["", "0", "false", "no", "true", "2", "01", " 1", "1 ", "yes", "1.0"],
    )
    def test_only_the_exact_value_one_enables(
        self, value: str, tmp_path: Path, monkeypatch
    ) -> None:
        """``bool(os.environ.get(VAR))`` would treat every one of these as ON,
        which is how a gated feature ends up filling a disk in production."""
        monkeypatch.setenv(TAPE_RECORD_ENV_VAR, value)
        target = tmp_path / "tapes"

        assert tape_recording_enabled() is False
        assert open_tape_recorder(broker="dhan", directory=target) is None
        assert not target.exists()

    def test_direct_construction_is_refused_when_disabled(self, tmp_path: Path) -> None:
        """The factory returning None is not the whole control: anyone can
        construct the class. Constructing it must also be refused, so there is
        no bypass around the gate."""
        path = tmp_path / "tape-dhan.jsonl"

        with pytest.raises(TapeRecordingDisabled):
            TapeRecorder(path, broker="dhan")
        assert not path.exists()
        assert list(tmp_path.iterdir()) == []

    def test_direct_construction_succeeds_when_enabled(
        self, tmp_path: Path, monkeypatch
    ) -> None:
        """The negative control for the refusal above: the same construction
        with the gate open writes normally."""
        monkeypatch.setenv(TAPE_RECORD_ENV_VAR, "1")
        path = tmp_path / "tape-dhan.jsonl"

        with TapeRecorder(path, broker="dhan") as recorder:
            recorder.record(_quote(datetime(2026, 9, 24, 4, 45, tzinfo=UTC)))

        assert _lines(path) != []


# -- the record format -------------------------------------------------------


class TestRecordFormat:
    def test_line_is_exactly_the_five_named_fields(
        self, tmp_path: Path, monkeypatch
    ) -> None:
        monkeypatch.setenv(TAPE_RECORD_ENV_VAR, "1")
        path = tmp_path / "tape-dhan.jsonl"

        with TapeRecorder(path, broker="dhan") as recorder:
            recorder.record(
                _quote(datetime(2026, 9, 24, 4, 45, 30, 123456, tzinfo=UTC))
            )

        line = _lines(path)[0]
        assert set(line) == {"broker", "symbol", "ts", "price", "qty"}
        assert line["broker"] == "dhan"
        assert line["symbol"] == "NSE:RELIANCE"
        assert line["price"] == "2500.50"
        assert line["qty"] == "1000"

    def test_price_and_qty_are_decimal_strings_not_json_floats(
        self, tmp_path: Path, monkeypatch
    ) -> None:
        """A float in this field would cost precision on the way back
        (``Decimal(str(2500.50))`` and a JSON round-trip of the float disagree),
        and these two requirements assert on fill prices."""
        monkeypatch.setenv(TAPE_RECORD_ENV_VAR, "1")
        path = tmp_path / "tape-dhan.jsonl"

        with TapeRecorder(path, broker="dhan") as recorder:
            recorder.record(
                _quote(datetime(2026, 9, 24, 4, 45, tzinfo=UTC), "1234.5678")
            )

        raw = path.read_text().splitlines()[0]
        assert '"price":"1234.5678"' in raw
        assert '"price":1234.5678' not in raw
        (tick,) = read_tape(path)
        assert isinstance(tick.price, Decimal)
        assert str(tick.price) == "1234.5678"

    def test_absent_volume_is_null_not_zero(
        self, tmp_path: Path, monkeypatch
    ) -> None:
        """A quote with no volume is recorded as null. Writing 0 would invent
        data the venue never sent, and a zero volume reads as 'traded and flat'."""
        monkeypatch.setenv(TAPE_RECORD_ENV_VAR, "1")
        path = tmp_path / "tape-dhan.jsonl"

        with TapeRecorder(path, broker="dhan") as recorder:
            recorder.record(
                _quote(datetime(2026, 9, 24, 4, 45, tzinfo=UTC), volume=None)
            )

        assert _lines(path)[0]["qty"] is None
        (tick,) = read_tape(path)
        assert tick.qty is None

    def test_present_volume_is_kept_so_null_is_not_the_default(
        self, tmp_path: Path, monkeypatch
    ) -> None:
        """The negative control for the null case above."""
        monkeypatch.setenv(TAPE_RECORD_ENV_VAR, "1")
        path = tmp_path / "tape-dhan.jsonl"

        with TapeRecorder(path, broker="dhan") as recorder:
            recorder.record(_quote(datetime(2026, 9, 24, 4, 45, tzinfo=UTC)))

        (tick,) = read_tape(path)
        assert tick.qty == Decimal("1000")

    def test_timestamp_is_ist_carrying_its_offset(
        self, tmp_path: Path, monkeypatch
    ) -> None:
        """IST is the platform's storage convention (CLAUDE.md), and the offset
        is written so the value is unambiguous on the way back — a naive
        timestamp is read as UTC somewhere and costs 5h30m."""
        monkeypatch.setenv(TAPE_RECORD_ENV_VAR, "1")
        path = tmp_path / "tape-dhan.jsonl"
        ts = datetime(2026, 9, 24, 4, 45, 30, 123456, tzinfo=UTC)

        with TapeRecorder(path, broker="dhan") as recorder:
            recorder.record(_quote(ts))

        assert _lines(path)[0]["ts"] == "2026-09-24T10:15:30.123456+05:30"
        (tick,) = read_tape(path)
        assert tick.ts == ts
        assert tick.ts == ts.astimezone(IST)
        assert tick.ts.tzinfo is IST
        assert tick.ts_ist == ts.astimezone(IST).replace(tzinfo=None)

    def test_naive_quote_timestamp_is_read_as_utc_then_stored_ist(
        self, tmp_path: Path, monkeypatch
    ) -> None:
        """Brokers that omit an offset report UTC, so a naive tick is UTC
        before it is IST."""
        monkeypatch.setenv(TAPE_RECORD_ENV_VAR, "1")
        path = tmp_path / "tape-dhan.jsonl"

        with TapeRecorder(path, broker="dhan") as recorder:
            recorder.record(_quote(datetime(2026, 9, 24, 4, 45)))

        (tick,) = read_tape(path)
        assert tick.ts == datetime(2026, 9, 24, 4, 45, tzinfo=UTC)
        assert tick.ts_ist == datetime(2026, 9, 24, 10, 15)

    def test_broker_is_the_venue_that_sent_the_quote(
        self, tmp_path: Path, monkeypatch
    ) -> None:
        """The record is evidence of what a venue sent, so the venue wins over
        the label the recorder was configured with; the label is the fallback
        for a quote that carries no provider."""
        monkeypatch.setenv(TAPE_RECORD_ENV_VAR, "1")
        path = tmp_path / "tape-dhan.jsonl"

        with TapeRecorder(path, broker="configured-label") as recorder:
            recorder.record(_quote(datetime(2026, 9, 24, 4, 45, tzinfo=UTC)))
            recorder.record(
                _quote(datetime(2026, 9, 24, 4, 46, tzinfo=UTC), provider="upstox")
            )
            recorder.record(
                _quote(datetime(2026, 9, 24, 4, 47, tzinfo=UTC), provider="")
            )

        assert [line["broker"] for line in _lines(path)] == [
            "dhan",
            "upstox",
            "configured-label",
        ]

    def test_filename_label_cannot_escape_the_tape_directory(
        self, tmp_path: Path, monkeypatch
    ) -> None:
        """The stem is built from a caller-supplied label, so a label is not
        allowed to decide where the file lands. ``x/../../..`` is the payload
        that actually escapes — a leading ``../..`` is cancelled by the
        ``tape-`` prefix's own component and stays inside, which is exactly why
        the obvious test for this passes against an unguarded writer."""
        monkeypatch.setenv(TAPE_RECORD_ENV_VAR, "1")
        target = tmp_path / "tapes"

        with open_tape_recorder(
            broker="x/../../../../escape", directory=target
        ) as recorder:
            assert recorder is not None
            assert recorder.path.parent == target.resolve()
            assert recorder.path.parent not in {tmp_path, tmp_path.parent}

    def test_a_label_that_sanitises_to_nothing_still_names_a_file(
        self, tmp_path: Path, monkeypatch
    ) -> None:
        """A label of dots or slashes must not produce ``tape-.jsonl`` or a
        name the filesystem rejects."""
        monkeypatch.setenv(TAPE_RECORD_ENV_VAR, "1")
        for label in ("..", ".", "///", " "):
            target = tmp_path / "tapes"
            with open_tape_recorder(broker=label, directory=target) as recorder:
                assert recorder is not None
                assert recorder.path.name.startswith("tape-unknown-")
                assert recorder.path.name.endswith(".jsonl")

    def test_the_default_file_is_named_for_the_ist_date_and_a_session_overrides_it(
        self, tmp_path: Path, monkeypatch
    ) -> None:
        """The day is part of the name so a restart appends to the same session
        and tomorrow starts a new one, and a caller that knows the session id
        can name it directly."""
        monkeypatch.setenv(TAPE_RECORD_ENV_VAR, "1")
        today = datetime.now(IST).strftime("%Y%m%d")
        target = tmp_path / "tapes"

        with open_tape_recorder(broker="dhan", directory=target) as recorder:
            assert recorder is not None
            assert recorder.path.name == f"tape-dhan-{today}.jsonl"

        with open_tape_recorder(
            broker="dhan", directory=target, session="run-42"
        ) as recorder:
            assert recorder is not None
            assert recorder.path.name == "tape-dhan-run-42.jsonl"

    def test_read_tape_stem_finds_the_rotated_naming(
        self, tmp_path: Path, monkeypatch
    ) -> None:
        """The stem form is what a session reader calls, so the file it names
        must be the one the writer produced."""
        monkeypatch.setenv(TAPE_RECORD_ENV_VAR, "1")
        target = tmp_path / "tapes"
        with open_tape_recorder(broker="dhan", directory=target) as recorder:
            assert recorder is not None
            base = recorder.base_path
            assert recorder.path == base

        assert base.name.startswith("tape-dhan")
        assert base.suffix == ".jsonl"
        assert read_tape_stem(base) == ()


# -- the reader's fidelity ---------------------------------------------------


class TestReaderFidelity:
    def _write_raw(self, path: Path, rows: list[dict]) -> Path:
        path.write_text("".join(json.dumps(row) + "\n" for row in rows))
        return path

    def _row(self, second: int, *, ltp: str = "2500.50") -> dict:
        return {
            "broker": "dhan",
            "symbol": "NSE:RELIANCE",
            "ts": f"2026-09-24T10:15:{second:02d}+05:30",
            "price": ltp,
            "qty": "1000",
        }

    def test_reader_preserves_out_of_order_and_duplicate_timestamps(
        self, tmp_path: Path
    ) -> None:
        """The recorded order is the order the venue sent. Sorting by
        timestamp, or de-duplicating, would quietly change the session the
        parity assertions are about — a duplicate tick becomes a missing one."""
        seconds = [30, 15, 22, 15, 45]
        path = self._write_raw(tmp_path / "tape.jsonl", [self._row(s) for s in seconds])

        ticks = read_tape(path)

        assert len(ticks) == 5
        assert [t.ts_ist.second for t in ticks] == seconds
        assert [t.ts_ist.minute for t in ticks] == [15] * 5

    def test_duplicate_ticks_survive_as_two_records(self, tmp_path: Path) -> None:
        """Two identical lines are two ticks. A set or a dict keyed on ts would
        collapse them to one and the fill sequence would lose a fill."""
        path = self._write_raw(tmp_path / "tape.jsonl", [self._row(15), self._row(15)])

        ticks = read_tape(path)

        assert len(ticks) == 2
        assert ticks[0] == ticks[1]

    def test_whole_recorded_session_round_trips_field_for_field(
        self, tmp_path: Path, monkeypatch
    ) -> None:
        """End to end through the writer and the reader, in the order written."""
        monkeypatch.setenv(TAPE_RECORD_ENV_VAR, "1")
        path = tmp_path / "tape-dhan.jsonl"
        written = [
            _quote(datetime(2026, 9, 24, 4, 45, s, tzinfo=UTC), ltp=ltp)
            for s, ltp in ((30, "2500.50"), (15, "2501.00"), (22, "2502.50"))
        ]
        with TapeRecorder(path, broker="dhan") as recorder:
            for quote in written:
                recorder.record(quote)

        ticks = read_tape(path)

        assert len(ticks) == len(written)
        for tick, quote in zip(ticks, written, strict=True):
            assert tick.symbol == "NSE:RELIANCE"
            assert tick.price == quote.ltp.value
            assert tick.qty == quote.volume.value
            assert tick.ts == quote.timestamp
            assert tick.broker == "dhan"

    def test_unknown_keys_are_preserved_for_a_later_wave(self, tmp_path: Path) -> None:
        """The tape is the first slice of a market data tape, and the obvious
        next fields are bid/ask. A reader that rejected unknown keys would have
        to be rewritten to take them; one that drops them loses them on replay."""
        path = self._write_raw(
            tmp_path / "tape.jsonl",
            [{**self._row(15), "bid": "2500.00", "ask": "2501.00"}],
        )

        (tick,) = read_tape(path)

        assert tick.extra == {"bid": "2500.00", "ask": "2501.00"}
        assert tick.price == Decimal("2500.50")

    def test_malformed_line_refuses_the_read_instead_of_skipping(
        self, tmp_path: Path
    ) -> None:
        """The tape is the only evidence of the session. A corrupt line in the
        middle is a hole in the fill sequence, and skipping it silently is how
        a parity assertion comes to mean nothing."""
        path = tmp_path / "tape.jsonl"
        path.write_text(
            json.dumps(self._row(15)) + "\n"
            + "{not json at all\n"
            + json.dumps(self._row(45)) + "\n"
        )

        with pytest.raises(TapeReadError) as excinfo:
            read_tape(path)

        assert "line 2" in str(excinfo.value)

    def test_truncated_final_line_is_named_as_a_truncation(
        self, tmp_path: Path
    ) -> None:
        """A process killed mid-write leaves a partial line. The read still
        fails rather than guessing, but the message has to say that is what it
        is likely to be, or an operator debugs a corrupt file that is not one."""
        path = tmp_path / "tape.jsonl"
        path.write_text(json.dumps(self._row(15)) + "\n" + '{"broker": "dhan", "sym')

        with pytest.raises(TapeReadError) as excinfo:
            read_tape(path)

        assert "line 2" in str(excinfo.value)
        assert "truncat" in str(excinfo.value).lower()

    def test_missing_required_field_refuses_the_read(self, tmp_path: Path) -> None:
        """A record without a price is not a tick. Defaulting it would invent
        the one number a fill is asserted on."""
        row = self._row(15)
        del row["price"]
        path = self._write_raw(tmp_path / "tape.jsonl", [row])

        with pytest.raises(TapeReadError) as excinfo:
            read_tape(path)

        assert "price" in str(excinfo.value)

    def test_unparseable_number_refuses_the_read(self, tmp_path: Path) -> None:
        path = self._write_raw(
            tmp_path / "tape.jsonl", [{**self._row(15), "price": "not-a-number"}]
        )

        with pytest.raises(TapeReadError) as excinfo:
            read_tape(path)

        assert "price" in str(excinfo.value)


# -- flush, close, rotation --------------------------------------------------


class TestDurability:
    def test_ticks_are_on_disk_while_the_recorder_is_still_open(
        self, tmp_path: Path, monkeypatch
    ) -> None:
        """A process killed mid session never runs close, so a recorder that
        only flushes on close loses the entire tape — the one artifact that
        exists to survive exactly that. Reading the open file is the control."""
        monkeypatch.setenv(TAPE_RECORD_ENV_VAR, "1")
        path = tmp_path / "tape-dhan.jsonl"

        recorder = TapeRecorder(path, broker="dhan")
        try:
            recorder.record(_quote(datetime(2026, 9, 24, 4, 45, tzinfo=UTC)))
            recorder.record(_quote(datetime(2026, 9, 24, 4, 45, 1, tzinfo=UTC)))

            assert len(_lines(path)) == 2
        finally:
            recorder.close()

    def test_flush_leaves_the_file_complete(self, tmp_path: Path, monkeypatch) -> None:
        monkeypatch.setenv(TAPE_RECORD_ENV_VAR, "1")
        path = tmp_path / "tape-dhan.jsonl"

        with TapeRecorder(path, broker="dhan") as recorder:
            recorder.record(_quote(datetime(2026, 9, 24, 4, 45, tzinfo=UTC)))
            recorder.flush()

        assert len(read_tape(path)) == 1

    def test_close_is_idempotent_and_stops_writing(self, tmp_path: Path, monkeypatch) -> None:
        """``close()`` runs from shutdown paths that may already have closed the
        recorder; a second close raising would turn a clean shutdown into a
        traceback. A write after close raises instead of being dropped."""
        monkeypatch.setenv(TAPE_RECORD_ENV_VAR, "1")
        path = tmp_path / "tape-dhan.jsonl"
        recorder = TapeRecorder(path, broker="dhan")
        recorder.record(_quote(datetime(2026, 9, 24, 4, 45, tzinfo=UTC)))

        recorder.close()
        recorder.close()

        with pytest.raises(RuntimeError):
            recorder.record(_quote(datetime(2026, 9, 24, 4, 46, tzinfo=UTC)))
        assert len(read_tape(path)) == 1

    def test_rotation_flushes_the_previous_file_and_keeps_order(
        self, tmp_path: Path, monkeypatch
    ) -> None:
        """Rotation is the other place a tape can be lost: a segment left
        unflushed, or a tick dropped at the seam. The recorded order must
        survive the whole series, and the stem reader must return it."""
        monkeypatch.setenv(TAPE_RECORD_ENV_VAR, "1")
        path = tmp_path / "tape-dhan.jsonl"
        recorder = TapeRecorder(path, broker="dhan", max_bytes=1)
        stamps = [datetime(2026, 9, 24, 4, 45, s, tzinfo=UTC) for s in range(6)]
        for ts in stamps:
            recorder.record(_quote(ts))
        recorder.close()

        segments = sorted(p.name for p in tmp_path.iterdir())
        assert len(segments) > 2, f"expected a rotated series, got {segments}"
        assert all(name.startswith("tape-dhan") for name in segments)

        ticks = read_tape_stem(path)
        assert [t.ts for t in ticks] == stamps
        assert [t.ts for t in read_tape(path)] == stamps[:1]

    def test_a_restart_appends_to_the_segments_session_not_truncating_it(
        self, tmp_path: Path, monkeypatch
    ) -> None:
        """A process killed mid session, restarted, has to keep what the first
        one recorded. Opening the day's tape for writing therefore appends: a
        truncating open would throw away the only surviving evidence of the
        minutes before the kill, and the restart is the ordinary case the
        durability requirement exists for."""
        monkeypatch.setenv(TAPE_RECORD_ENV_VAR, "1")
        path = tmp_path / "tape-dhan-20260924.jsonl"
        early = datetime(2026, 9, 24, 4, 45, tzinfo=UTC)
        late = datetime(2026, 9, 24, 4, 46, tzinfo=UTC)

        with TapeRecorder(path, broker="dhan") as first:
            first.record(_quote(early))
        with TapeRecorder(path, broker="dhan") as second:
            second.record(_quote(late))

        assert [t.ts for t in read_tape(path)] == [early, late]

    def test_the_previous_segment_is_complete_while_the_next_one_is_open(
        self, tmp_path: Path, monkeypatch
    ) -> None:
        """Rotation is where a segment can be left short. The sealed segment is
        read here without closing the recorder, so a rotation that moved on
        without finishing the previous file shows up as a short read."""
        monkeypatch.setenv(TAPE_RECORD_ENV_VAR, "1")
        path = tmp_path / "tape-dhan.jsonl"
        stamps = [datetime(2026, 9, 24, 4, 45, s, tzinfo=UTC) for s in (0, 1)]
        recorder = TapeRecorder(path, broker="dhan", max_bytes=1)
        try:
            recorder.record(_quote(stamps[0]))
            assert len(read_tape(path)) == 1

            recorder.record(_quote(stamps[1]))
            # Already two rotations deep, each write having sealed its segment.
            assert recorder.path.name == "tape-dhan.0002.jsonl"
            assert len(read_tape(path)) == 1
            assert [t.ts for t in read_tape_stem(path)] == stamps
        finally:
            recorder.close()

    def test_rotation_of_an_empty_file_does_not_create_a_segment(
        self, tmp_path: Path, monkeypatch
    ) -> None:
        """A forced rotate on nothing would leave an empty file for a reader to
        step over, and a session whose first tick is its second segment is a
        session nobody reads."""
        monkeypatch.setenv(TAPE_RECORD_ENV_VAR, "1")
        path = tmp_path / "tape-dhan.jsonl"

        with TapeRecorder(path, broker="dhan") as recorder:
            recorder.rotate()

        assert [p.name for p in tmp_path.iterdir()] == ["tape-dhan.jsonl"]

    def test_concurrent_records_do_not_interleave_a_line(
        self, tmp_path: Path, monkeypatch
    ) -> None:
        """Ticks arrive on the quote and depth receive threads; an unwritten
        write would splice two ticks into one line, which the reader then
        refuses and takes the whole session with it."""
        monkeypatch.setenv(TAPE_RECORD_ENV_VAR, "1")
        path = tmp_path / "tape-dhan.jsonl"
        per_thread = 200

        with TapeRecorder(path, broker="dhan") as recorder:

            def _write(offset: int) -> None:
                base = datetime(2026, 9, 24, 4, 45, tzinfo=UTC)
                for i in range(per_thread):
                    recorder.record(
                        _quote(
                            base + timedelta(seconds=offset * per_thread + i),
                            ltp=f"{2500 + offset}.{i:02d}",
                        )
                    )

            threads = [threading.Thread(target=_write, args=(n,)) for n in range(4)]
            for thread in threads:
                thread.start()
            for thread in threads:
                thread.join()

        assert len(read_tape(path)) == 4 * per_thread
