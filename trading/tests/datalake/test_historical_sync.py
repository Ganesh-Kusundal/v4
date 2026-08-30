"""Tests for HistoricalSyncService — three-phase gap-aware orchestration."""

from __future__ import annotations

import pandas as pd
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from unittest.mock import MagicMock, patch

import pytest

from tradex_domain import OHLC, Candle, Equity, Timeframe
from tradex_domain.market import HistoricalSeries
from tradex_domain.value_objects import Price, Quantity

from tradex_trading.datalake.historical_sync import (
    BLACKLIST_COOLDOWN_DAYS,
    HistoricalSyncService,
    SyncResult,
    _series_to_frame,
)

import json

INSTRUMENTS = [Equity.of("NSE", f"SYM{i}") for i in range(5)]
BASE = datetime(2026, 8, 1, 9, 15, tzinfo=UTC)


def _series(n=10, instrument=None) -> HistoricalSeries:
    inst = instrument or INSTRUMENTS[0]
    candles = [
        Candle(
            instrument=inst,
            timeframe=Timeframe.M1,
            ohlc=OHLC(open=Price(Decimal("100")), high=Price(Decimal("101")),
                    low=Price(Decimal("99")), close=Price(Decimal("100"))),
            volume=Quantity(Decimal("1000")),
            timestamp=BASE + timedelta(minutes=i),
        )
                for i in range(n)
    ]
    return HistoricalSeries(
        instrument=inst, timeframe=Timeframe.M1,
        candles=candles, start=BASE, end=BASE + timedelta(minutes=n - 1),
    )


def _mock_fetcher_factory(return_series=None, fail_symbols=None):
    """Build a mock fetcher whose .fetch returns {inst_id: HistoricalSeries}."""
    fail_symbols = fail_symbols or set()
    series = return_series or _series

    def _fetch(instruments, tf, start, end, ranges=None):
        results = {}
        for inst in instruments:
            if str(inst.instrument_id) in fail_symbols:
                continue
            results[str(inst.instrument_id)] = series(instrument=inst)
        return results

    fetcher = MagicMock()
    fetcher.fetch = MagicMock(side_effect=_fetch)
    return fetcher


def _mock_detector(gapped_symbols=None):
    """Build a mock GapDetector reporting *gapped_symbols* as gapped."""
    gapped = gapped_symbols or set()

    def _detect(instruments, start, end, **kwargs):
        return [
            (inst, [(start, end)])
            for inst in instruments
            if inst.symbol in gapped
        ]

    detector = MagicMock()
    detector.detect = MagicMock(side_effect=_detect)
    return detector


def _mock_store(total_rows=100):
    """Build a mock ParquetStorage with upsert/read mocked."""
    store = MagicMock()
    store.upsert = MagicMock(return_value=total_rows)
    store.read = MagicMock(return_value=pd.DataFrame())
    store.symbols = MagicMock(return_value=[])
    return store


def _mock_broker(name: str, same_day: bool = False) -> MagicMock:
    """Mock broker serving a per-instrument series (for phase 2/3 fetchers).

    Carries a real (fail-closed) capability table so same-day broker
    selection matches production behavior.
    """
    from tradex_domain.capabilities import BrokerCapabilities

    broker = MagicMock()
    broker._capabilities = BrokerCapabilities(
        supports_same_day_intraday=same_day,
    )

    def _history(inst, tf, start, end):
        return _series(instrument=inst)

    broker.history = MagicMock(side_effect=_history)
    broker.name = name
    return broker


def _service(fetcher=None, detector=None, store=None) -> HistoricalSyncService:
    return HistoricalSyncService(
        store=store or _mock_store(),
        detector=detector or _mock_detector(),
        fetcher=fetcher,
    )


@pytest.fixture(autouse=True)
def _stub_universe(tmp_path, monkeypatch):
    """Pin sync()/verify() to the module's 5-symbol test universe and keep
    the blacklist file inside the per-test tmp dir."""
    from tradex_trading.datalake import historical_sync as hs_mod

    monkeypatch.setattr(
        hs_mod, "_DEFAULT_BLACKLIST_PATH",
        tmp_path / ".sync_blacklist.json",
    )
    with patch(
        "tradex_trading.datalake.historical_sync.load_universe",
        return_value=INSTRUMENTS,
    ):
        yield


# --------------------------------------------------------------------------- #
# _series_to_frame (pure conversion helper)
# --------------------------------------------------------------------------- #

class TestSeriesToFrame:
    def test_converts_candles_to_storage_columns(self):
        df = _series_to_frame(_series(n=3), "SYM0")
        assert list(df.columns) == [
            "symbol", "exchange", "kind", "timeframe",
            "timestamp", "open", "high", "low", "close", "volume",
        ]
        assert len(df) == 3
        assert df["symbol"].eq("SYM0").all()
        assert df["kind"].eq("equity").all()
        assert df["timestamp"].dt.tz is None  # tz-naive IST stamps

    def test_empty_series_gives_empty_frame(self):
        empty = HistoricalSeries(
            instrument=INSTRUMENTS[0], timeframe=Timeframe.M1,
            candles=[], start=BASE, end=BASE,
        )
        assert _series_to_frame(empty, "SYM0").empty


class TestPhaseOne:
    def test_no_gaps_means_no_fetch(self):
        """Clean datalake -> phase 1 fetches nothing, writes nothing."""
        fetcher = _mock_fetcher_factory()
        svc = _service(fetcher=fetcher)
        result = svc.sync(brokers={"dhan": _mock_broker("dhan")})
        assert isinstance(result, SyncResult)
        assert result.fetched == 0
        assert result.written == 0
        assert result.gaps_remaining == 0
        fetcher.fetch.assert_not_called()

    def test_only_gapped_symbols_fetched(self):
        """Phase 1 narrows the universe to symbols the detector flags."""
        detector = _mock_detector({"SYM2", "SYM4"})
        fetcher = _mock_fetcher_factory()
        svc = _service(fetcher=fetcher, detector=detector)
        result = svc.sync(brokers={"dhan": _mock_broker("dhan")})
        assert result.requested == 5
        assert result.fetched == 2
        fetched_insts = fetcher.fetch.call_args[0][0]
        assert {i.symbol for i in fetched_insts} == {"SYM2", "SYM4"}
        assert result.written > 0

    def test_result_reports_residual_gaps_and_failed(self):
        """Verification re-run feeds gaps_remaining/failed into SyncResult."""
        detector = _mock_detector({"SYM1"})
        svc = _service(detector=detector)
        result = svc.sync(brokers={"dhan": _mock_broker("dhan")})
        assert result.gaps_remaining == 1
        assert result.failed == ["SYM1"]

    def test_verify_skipped_when_disabled(self):
        detector = _mock_detector({"SYM1"})
        svc = _service(detector=detector)
        # pre-open pin: phases 2/3 cannot add stray detect calls
        with patch(
            "tradex_trading.datalake.historical_sync.datetime",
            _PreOpenDatetime,
        ):
            result = svc.sync(
                brokers={"dhan": _mock_broker("dhan")}, filler_broker=None,
                verify=False,
            )
        assert result.gaps_remaining == 0
        assert result.failed == []
        assert detector.detect.call_count == 1  # only the phase-1 filter ran


# --------------------------------------------------------------------------- #
# Phase 2 — filler top-up
# --------------------------------------------------------------------------- #

class TestPhaseTwoFiller:
    def test_filler_refetches_still_gapped_symbols(self):
        """Symbols still gapped after phase 1 are refetched via the filler."""
        # detect call order: phase-1 filter -> SYM0; post-phase-1 -> SYM0+SYM1
        # still gapped; final verification -> clean.
        detector = MagicMock()
        detector.detect.side_effect = [
            [(INSTRUMENTS[0], [(BASE, BASE)])],
            [(INSTRUMENTS[0], [(BASE, BASE)]),
             (INSTRUMENTS[1], [(BASE, BASE)])],
            [],
        ]
        upstox = _mock_broker("upstox")
        svc = _service(detector=detector)
        result = svc.sync(
            brokers={"upstox": upstox},
            filler_broker="upstox", verify=True,
        )
        assert upstox.history.call_count >= 2  # filler served both symbols
        assert result.written > 0

    def test_filler_skipped_when_not_among_brokers(self):
        """filler_broker not in brokers dict -> phase 2 never runs."""
        detector = _mock_detector({"SYM0"})
        dhan = _mock_broker("dhan")
        svc = _service(detector=detector)
        svc.sync(brokers={"dhan": dhan}, filler_broker="upstox", verify=False)
        assert dhan.history.call_count >= 1  # only phase-1 traffic happened

    def test_injected_fetcher_used_for_phase_one(self):
        fetcher = _mock_fetcher_factory()
        svc = _service(fetcher=fetcher, detector=_mock_detector({"SYM0"}))
        svc.sync(brokers={"dhan": _mock_broker("dhan")})
        fetcher.fetch.assert_called_once()


# --------------------------------------------------------------------------- #
# Phase 3 — same-day top-up
# --------------------------------------------------------------------------- #

class TestPhaseThreeSameDay:
    def test_same_day_topup_via_dhan(self):
        """Today's bars still missing after filler -> Dhan tops them up."""
        full_gap = (INSTRUMENTS[0], [(BASE, BASE)])
        detector = MagicMock()
        detector.detect.side_effect = [
            [full_gap],   # phase-1 filter
            [],           # phase-2 residual check: filler fixed history
            [full_gap],   # phase-3 today check
            [],           # verification
        ]
        dhan = _mock_broker("dhan", same_day=True)
        upstox = _mock_broker("upstox")
        svc = _service(detector=detector)
        with patch(
            "tradex_trading.datalake.historical_sync.datetime",
            _FixedDatetime,
        ):
            svc.sync(brokers={"dhan": dhan, "upstox": upstox})
        assert dhan.history.call_count >= 1

    def test_phase3_skipped_when_dhan_absent(self):
        """Only Upstox configured -> no same-day broker, phase 3 inert."""
        detector = _mock_detector({"SYM0"})
        upstox = _mock_broker("upstox")
        svc = _service(detector=detector)
        result = svc.sync(brokers={"upstox": upstox}, verify=False)
        assert isinstance(result, SyncResult)

    def test_phase3_guarded_before_session_open(self):
        """now < 09:00 IST -> no detect call for the today window."""
        detector = MagicMock()
        detector.detect.side_effect = [
            [(INSTRUMENTS[0], [(BASE, BASE)])],  # phase-1 filter
            [],                                   # phase-2 residual
        ]
        dhan = _mock_broker("dhan", same_day=True)
        svc = _service(detector=detector)
        with patch(
            "tradex_trading.datalake.historical_sync.datetime",
            _PreOpenDatetime,
        ):
            svc.sync(brokers={"dhan": dhan}, filler_broker=None,
                     verify=False)
        # 1 detect only: the phase-1 filter; phases 2/3 never ran
        assert detector.detect.call_count == 1


# --------------------------------------------------------------------------- #
# Window computation — IST-naive regardless of host clock
# --------------------------------------------------------------------------- #

class _FixedDatetime(datetime):
    """datetime stub pinned to 10:00 UTC == 15:30 IST."""

    @classmethod
    def now(cls, tz=None):
        return datetime(2026, 8, 26, 10, 0, tzinfo=UTC)


class _PreOpenDatetime(datetime):
    """datetime stub pinned to 02:30 UTC == 08:00 IST (pre-open)."""

    @classmethod
    def now(cls, tz=None):
        return datetime(2026, 8, 26, 2, 30, tzinfo=UTC)


class TestWindowsAreIstNaive:
    def test_sync_windows_use_ist_wall_clock(self):
        """Detector receives tz-naive IST bounds derived from UTC now."""
        detector = _mock_detector()
        svc = _service(detector=detector)
        with patch(
            "tradex_trading.datalake.historical_sync.datetime",
            _FixedDatetime,
        ):
            svc.sync(brokers={"dhan": _mock_broker("dhan")}, months=1)
        kwargs = detector.detect.call_args[1]
        start, end = kwargs["start"], kwargs["end"]
        assert end.tzinfo is None and start.tzinfo is None
        assert (end.hour, end.minute) == (15, 30)   # 10:00 UTC == 15:30 IST
        assert (start.hour, start.minute) == (0, 0)  # midnight-aligned
        assert (end - start).days == 30              # months * 30

    def test_verify_windows_use_ist_wall_clock(self):
        detector = _mock_detector()
        svc = _service(detector=detector)
        with patch(
            "tradex_trading.datalake.historical_sync.datetime",
            _FixedDatetime,
        ):
            svc.verify(months=2)
        kwargs = detector.detect.call_args[1]
        assert (kwargs["end"].hour, kwargs["end"].minute) == (15, 30)
        assert (kwargs["end"] - kwargs["start"]).days == 60


# --------------------------------------------------------------------------- #
# verify() — read-only report
# --------------------------------------------------------------------------- #

class TestVerify:
    def test_counts_bad_ohlc_rows_in_sample(self):
        bad = pd.DataFrame({
            "high": [90.0, 101.0],  # row 0 violates high >= open/close
            "open": [100.0, 100.0],
            "close": [100.0, 100.0],
            "low": [99.0, 99.0],
        })
        store = _mock_store()
        store.read = MagicMock(return_value=bad)
        svc = _service(store=store)
        report = svc.verify()
        assert report["ohlc_bad_rows"] == 1
        assert report["total_rows"] == 2
        assert report["requested"] == len(INSTRUMENTS)

    def test_clean_sample_reports_zero_bad(self):
        svc = _service(store=_mock_store())
        assert svc.verify()["ohlc_bad_rows"] == 0


# --------------------------------------------------------------------------- #
# Incremental ranged fetch + gap-reduction reporting
# --------------------------------------------------------------------------- #

class TestRangedSync:
    def test_phase1_passes_detector_ranges_to_fetcher(self):
        """GapDetector's missing sub-windows flow through as ranges."""
        narrow = (BASE + timedelta(days=1), BASE + timedelta(days=2))
        detector = MagicMock()
        detector.detect.return_value = [(INSTRUMENTS[1], [narrow])]
        fetcher = _mock_fetcher_factory()
        svc = _service(fetcher=fetcher, detector=detector)
        result = svc.sync(brokers={"dhan": _mock_broker("dhan")},
                          filler_broker=None)
        kwargs = fetcher.fetch.call_args[1]
        assert set(kwargs["ranges"]) == {str(INSTRUMENTS[1].instrument_id)}
        assert kwargs["ranges"][str(INSTRUMENTS[1].instrument_id)] == [narrow]
        assert result.gaps_before == 1

    def test_gap_reduction_bracketed(self):
        detector = _mock_detector({"SYM1", "SYM2"})
        svc = _service(detector=detector)
        result = svc.sync(brokers={"dhan": _mock_broker("dhan")})
        assert result.gaps_before == 2
        assert result.gaps_remaining == 2  # mocks never write real bars


# --------------------------------------------------------------------------- #
# Failure blacklist — skip dead symbols, report them, auto-expire
# --------------------------------------------------------------------------- #

def _bl_path(tmp_path):
    return tmp_path / ".sync_blacklist.json"


class TestBlacklist:
    def test_still_gapped_symbol_gets_blacklisted(self, tmp_path):
        detector = _mock_detector({"SYM3"})
        svc = _service(detector=detector)
        result = svc.sync(brokers={"dhan": _mock_broker("dhan")})
        assert "SYM3" in result.failed
        bl = json.loads(_bl_path(tmp_path).read_text())
        assert bl["SYM3"]["fails"] == 1
        assert bl["SYM3"]["reason"] == "still_gapped_after_sync"

    def test_blacklisted_symbol_skipped_and_reported(self, tmp_path):
        _bl_path(tmp_path).write_text(json.dumps({
            "SYM0": {
                "fails": 2,
                "last_ts": (datetime.now(UTC) - timedelta(hours=1))
                .replace(tzinfo=None).isoformat(),
                "reason": "still_gapped_after_sync",
            },
        }))
        detector = _mock_detector()  # nothing gapped among remaining actives
        fetcher = _mock_fetcher_factory()
        svc = _service(fetcher=fetcher, detector=detector)
        result = svc.sync(brokers={"dhan": _mock_broker("dhan")})
        assert result.blacklisted == ["SYM0"]
        fetcher.fetch.assert_not_called()

    def test_cooldown_expiry_retries_and_replaces_entry(self, tmp_path):
        stale = (datetime.now(UTC) - timedelta(days=BLACKLIST_COOLDOWN_DAYS + 1))
        _bl_path(tmp_path).write_text(json.dumps({
            "SYM0": {"fails": 5, "last_ts": stale.replace(tzinfo=None).isoformat(),
                     "reason": "old"},
        }))
        detector = _mock_detector({"SYM0"})
        fetcher = _mock_fetcher_factory()
        svc = _service(fetcher=fetcher, detector=detector)
        result = svc.sync(brokers={"dhan": _mock_broker("dhan")})
        assert result.blacklisted == []       # expired -> retried this run
        assert fetcher.fetch.called
        bl = json.loads(_bl_path(tmp_path).read_text())
        assert bl["SYM0"]["fails"] == 1       # fresh entry replaced stale one

    def test_recovered_symbol_pruned(self, tmp_path):
        _bl_path(tmp_path).write_text(json.dumps({
            "SYM4": {
                "fails": 1,
                "last_ts": (datetime.now(UTC) - timedelta(hours=2))
                .replace(tzinfo=None).isoformat(),
                "reason": "still_gapped_after_sync",
            },
        }))
        detector = _mock_detector()           # SYM4 data arrived meanwhile
        svc = _service(detector=detector)
        result = svc.sync(brokers={"dhan": _mock_broker("dhan")})
        assert result.blacklisted == ["SYM4"]
        assert json.loads(_bl_path(tmp_path).read_text()) == {}  # pruned


# --------------------------------------------------------------------------- #
# Capability-driven same-day selection + holidays injection
# --------------------------------------------------------------------------- #

class TestCapabilityDrivenSameDay:
    def test_any_broker_with_capability_is_used(self):
        """Same-day top-up follows the capability table, not a hard-coded name."""
        full_gap = (INSTRUMENTS[0], [(BASE, BASE)])
        detector = MagicMock()
        detector.detect.side_effect = [
            [full_gap],   # phase-1 filter
            [],           # phase-2 residual
            [full_gap],   # phase-3 today check
            [],           # verification
        ]
        upstox = _mock_broker("upstox", same_day=True)
        svc = _service(detector=detector)
        with patch(
            "tradex_trading.datalake.historical_sync.datetime",
            _FixedDatetime,
        ):
            svc.sync(brokers={"upstox": upstox}, filler_broker=None)
        assert upstox.history.call_count >= 1

    def test_broker_without_capability_never_serves_same_day(self):
        detector = _mock_detector({"SYM0"})
        upstox = _mock_broker("upstox", same_day=False)
        svc = _service(detector=detector)
        with patch(
            "tradex_trading.datalake.historical_sync.datetime",
            _FixedDatetime,
        ):
            result = svc.sync(brokers={"upstox": upstox}, filler_broker=None,
                              verify=False)
        assert isinstance(result, SyncResult)


class TestHolidaysInjection:
    def test_holidays_param_flows_to_detector(self):
        custom = frozenset({"2026-01-26"})
        detector = MagicMock()
        detector.detect.return_value = []
        svc = HistoricalSyncService(store=_mock_store(), detector=detector,
                                    holidays=custom)
        svc.verify()
        assert detector.detect.call_args.kwargs["holidays"] == custom

