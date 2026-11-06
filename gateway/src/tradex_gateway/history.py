"""Historical bars: datalake-first for NSE spot equity, broker otherwise."""

from __future__ import annotations

import time
from collections.abc import Callable
from concurrent.futures import Executor
from datetime import UTC, datetime, timedelta
from typing import Any, cast

from tradex_config.broker_limits import BROKER_LIMITS
from tradex_domain.enums import AssetClass, Timeframe
from tradex_domain.instruments import Instrument
from tradex_domain.market import Candle, HistoricalSeries
from tradex_domain.timeframe import TIMEFRAMES
from tradex_domain.timezones import IST_ZONE
from tradex_market_data.window_loader import MarketSource, MarketWindowLoader

from tradex_gateway.cache import SingleFlight, TTLCache
from tradex_gateway.directory import InstrumentDirectory
from tradex_gateway.errors import BadRequest, GatewayError, UnsupportedTimeframe
from tradex_gateway.router import MarketDataRouter

_RESAMPLE_FROM: dict[Timeframe, Timeframe] = {
    Timeframe.M30: Timeframe.M15,
    Timeframe.H1: Timeframe.M15,
    Timeframe.D1: Timeframe.H1,
    Timeframe.W1: Timeframe.D1,
}


def to_epoch_ms(moment: datetime) -> int:
    if moment.tzinfo is None:
        moment = moment.replace(tzinfo=IST_ZONE)
    return int(moment.timestamp() * 1000)


def _to_utc(moment: datetime) -> datetime:
    if moment.tzinfo is None:
        moment = moment.replace(tzinfo=IST_ZONE)
    return moment.astimezone(UTC)


class HistoryService:
    def __init__(
        self,
        directory: InstrumentDirectory,
        router: MarketDataRouter,
        *,
        window_loader: MarketWindowLoader | None = None,
        clock: Callable[[], float] = time.monotonic,
        executor: Executor | None = None,
    ) -> None:
        self._directory = directory
        self._router = router
        self._loader = window_loader
        self._clock = clock
        self._executor = executor
        self._cache = TTLCache()
        self._flight = SingleFlight()

    def history(
        self,
        instrument_id: str,
        timeframe: str,
        start: datetime,
        end: datetime,
        *,
        source: str = "auto",
        broker: str | None = None,
        fill_gaps: bool = False,
    ) -> dict[str, Any]:
        if source not in {"auto", "datalake", "broker"}:
            raise BadRequest(f"invalid source: {source!r}")
        try:
            tf = Timeframe(timeframe)
        except ValueError as exc:
            raise UnsupportedTimeframe(f"unsupported timeframe: {timeframe!r}") from exc
        start_utc, end_utc = _to_utc(start), _to_utc(end)
        if end_utc <= start_utc:
            raise BadRequest("'to' must be later than 'from'")
        resolved = self._directory.resolve(instrument_id)
        instrument = resolved.instrument
        key = (
            str(resolved.instrument_id),
            tf.value,
            start_utc.isoformat(),
            end_utc.isoformat(),
            source,
            broker,
            fill_gaps,
        )
        now = self._clock()
        cached = self._cache.get(key, now)
        if cached is not None:
            return {**cached, "cached": True}
        with self._flight.lock(key):
            cached = self._cache.get(key, now)
            if cached is not None:
                return {**cached, "cached": True}
            result = self._load(
                instrument,
                tf,
                start_utc,
                end_utc,
                source=source,
                broker=broker,
                fill_gaps=fill_gaps,
            )
            ttl = 3600.0 if end_utc < datetime.now(UTC) - timedelta(days=1) else 5.0
            self._cache.put(key, result, ttl, now)
            return result

    def batch(
        self, requests: list[dict[str, Any]], *, fill_gaps: bool = False
    ) -> list[dict[str, Any]]:
        results: list[dict[str, Any]] = [{} for _ in requests]

        def run(index: int, payload: dict[str, Any]) -> None:
            try:
                end = _parse_moment(payload.get("to")) if payload.get("to") else datetime.now(UTC)
                start = (
                    _parse_moment(payload.get("from_"))
                    if payload.get("from_")
                    else end - timedelta(days=5)
                )
                result = self.history(
                    payload["instrument_id"],
                    payload.get("timeframe", "1m"),
                    start,
                    end,
                    source=payload.get("source", "auto"),
                    broker=payload.get("broker"),
                    fill_gaps=payload.get("fill_gaps", fill_gaps),
                )
                results[index] = result
            except GatewayError as exc:
                results[index] = {
                    "instrument_id": payload.get("instrument_id"),
                    **exc.to_envelope(),
                }
            except Exception as exc:  # noqa: BLE001 - one request must not sink the batch
                results[index] = {
                    "instrument_id": payload.get("instrument_id"),
                    "error": {
                        "code": "internal_error",
                        "message": str(exc),
                        "broker": None,
                        "retryable": False,
                        "retry_after": None,
                    },
                }

        if self._executor is None or not requests:
            # Direct callers (unit tests) without a pool: run in order, here.
            for index, payload in enumerate(requests):
                run(index, payload)
            return results

        # Every item goes to the injected executor, which is the dedicated
        # batch pool and never the request pool. That separation is the whole
        # safety argument: the route runs batch() itself on an
        # app.state.executor worker and batch() blocks there, so children
        # submitted into that pool would queue behind the very parents that
        # wait for them and nothing would ever complete. Results stay
        # index-aligned because each task writes its own slot.
        futures = [
            self._executor.submit(run, index, payload) for index, payload in enumerate(requests)
        ]
        for future in futures:
            future.result()  # run() converts every exception into an envelope
        return results

    def _load(
        self,
        instrument: Instrument,
        tf: Timeframe,
        start: datetime,
        end: datetime,
        *,
        source: str,
        broker: str | None,
        fill_gaps: bool,
    ) -> dict[str, Any]:
        if source != "broker" and _datalake_eligible(instrument):
            bars, datalake_source = self._from_datalake(instrument, tf, start, end)
            if bars:
                if fill_gaps:
                    bars = self._fill_tail(instrument, tf, bars, end, preferred=broker)
                return self._result(instrument, tf, start, end, bars, "datalake", None)
            if source == "datalake":
                return self._result(instrument, tf, start, end, [], "none", None)

        series, broker_name = self._router.call(
            "history",
            lambda adapter: self._broker_series(adapter, instrument, tf, start, end),
            preferred=broker,
        )
        bars = [_bar_dict(candle) for candle in series.candles]
        return self._result(instrument, tf, start, end, bars, "broker", broker_name)

    def _from_datalake(
        self, instrument: Instrument, tf: Timeframe, start: datetime, end: datetime
    ) -> tuple[list[dict[str, Any]], str]:
        if self._loader is None:
            self._loader = MarketWindowLoader()
        loader = self._loader
        try:
            window = loader.load(instrument, tf, start, end, allow_broker=False)
        except Exception:  # noqa: BLE001 - a lake miss must fall through to brokers
            return [], "none"
        if window.source is MarketSource.DATALAKE and window.candles:
            return [_bar_dict(candle) for candle in window.candles], "datalake"
        return [], "none"

    def _fill_tail(
        self,
        instrument: Instrument,
        tf: Timeframe,
        bars: list[dict[str, Any]],
        end: datetime,
        *,
        preferred: str | None,
    ) -> list[dict[str, Any]]:
        last_ms = bars[-1]["t"]
        end_ms = to_epoch_ms(end)
        if last_ms >= end_ms:
            return bars
        tail_start = datetime.fromtimestamp(last_ms / 1000, tz=UTC)
        try:
            series, _ = self._router.call(
                "history",
                lambda adapter: self._broker_series(adapter, instrument, tf, tail_start, end),
                preferred=preferred,
            )
        except GatewayError:
            return bars
        merged = {bar["t"]: bar for bar in bars}
        for candle in series.candles:
            bar = _bar_dict(candle)
            merged[bar["t"]] = bar
        return [merged[key] for key in sorted(merged)]

    def _broker_series(
        self,
        broker: Any,
        instrument: Instrument,
        tf: Timeframe,
        start: datetime,
        end: datetime,
    ) -> HistoricalSeries:
        try:
            return _fetch_chunks(broker, instrument, tf, start, end)
        except ValueError:
            fallback = _RESAMPLE_FROM.get(tf)
            if fallback is None:
                raise
            base = _fetch_chunks(broker, instrument, fallback, start, end)
            return base.resample(tf)

    def _result(
        self,
        instrument: Instrument,
        tf: Timeframe,
        start: datetime,
        end: datetime,
        bars: list[dict[str, Any]],
        source: str,
        broker: str | None,
    ) -> dict[str, Any]:
        expected = _expected_bars(tf, start, end)
        last_ms = bars[-1]["t"] if bars else None
        tolerance = 2 * _tf_millis(tf)
        complete = bool(bars) and last_ms is not None and last_ms >= to_epoch_ms(end) - tolerance
        return {
            "instrument": str(instrument.instrument_id),
            "timeframe": tf.value,
            "source": source,
            "broker": broker,
            "bars": bars,
            "coverage": {
                "from": start.isoformat(),
                "to": end.isoformat(),
                "requested": expected,
                "returned": len(bars),
                "complete": complete,
            },
            "degraded": source == "none",
            "cached": False,
        }


def _datalake_eligible(instrument: Instrument) -> bool:
    return instrument.asset_class is AssetClass.EQUITY and str(instrument.exchange) == "NSE"


def _fetch_chunks(
    broker: Any,
    instrument: Instrument,
    tf: Timeframe,
    start: datetime,
    end: datetime,
) -> HistoricalSeries:
    candles: list[Candle] = []
    step = timedelta(seconds=_tf_seconds(tf))
    max_history_days = getattr(getattr(broker, "capabilities", None), "max_history_days", None)
    for window_start, window_end in _windows(tf, start, end, max_history_days):
        series = broker.history(instrument, tf, window_start, window_end)
        # Brokers widen an intraday request to the whole day; keep only bars
        # that overlap the requested window. A bar is included when its span
        # (timestamp .. timestamp + timeframe) intersects it.
        candles.extend(
            candle
            for candle in series.candles
            if candle.timestamp + step > window_start and candle.timestamp <= window_end
        )
    unique: dict[datetime, Candle] = {}
    for candle in candles:
        unique[candle.timestamp] = candle
    ordered = [unique[key] for key in sorted(unique)]
    return HistoricalSeries(
        instrument=instrument,
        timeframe=tf,
        candles=ordered,
        start=start,
        end=end,
    )


def _windows(
    tf: Timeframe,
    start: datetime,
    end: datetime,
    max_history_days: int | None,
) -> list[tuple[datetime, datetime]]:
    intraday = bool(TIMEFRAMES[tf.value]["intraday"])
    if not intraday:
        cap_days = 3650
    elif isinstance(max_history_days, int) and max_history_days > 0:
        # Chunk at the cap the selected adapter declares for itself (Dhan 90d,
        # Upstox 30d) instead of the tightest shared limit.
        cap_days = max_history_days
    else:
        # The selected adapter declares no cap: fall back to the tightest
        # known broker limit so an intraday window is never over-wide.
        cap_days = min(BROKER_LIMITS.dhan_max_days, BROKER_LIMITS.upstox_max_days)
    windows: list[tuple[datetime, datetime]] = []
    cursor = start
    while cursor < end:
        next_stop = min(cursor + timedelta(days=cap_days), end)
        windows.append((cursor, next_stop))
        cursor = next_stop
    return windows or [(start, end)]


def _tf_seconds(tf: Timeframe) -> int:
    return cast(int, TIMEFRAMES[tf.value]["seconds"])


def _tf_millis(tf: Timeframe) -> int:
    return _tf_seconds(tf) * 1000


def _expected_bars(tf: Timeframe, start: datetime, end: datetime) -> int:
    seconds = max(1, _tf_seconds(tf))
    return max(1, int((end - start).total_seconds() // seconds) + 1)


def _bar_dict(candle: Candle) -> dict[str, Any]:
    return {
        "t": to_epoch_ms(candle.timestamp),
        "o": float(candle.ohlc.open.value),
        "h": float(candle.ohlc.high.value),
        "l": float(candle.ohlc.low.value),
        "c": float(candle.ohlc.close.value),
        "v": float(candle.volume.value),
    }


def _parse_moment(value: str | None) -> datetime:
    if not value:
        return datetime.now(UTC)
    text = value.strip().replace("Z", "+00:00")
    try:
        parsed = datetime.fromisoformat(text)
    except ValueError as exc:
        raise BadRequest(f"invalid timestamp: {value!r}") from exc
    if parsed.tzinfo is None:
        raise BadRequest("timestamp must include a UTC offset")
    return _to_utc(parsed)
