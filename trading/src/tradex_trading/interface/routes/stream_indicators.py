"""WS indicator push — Tier-2 subscribe seam for /ws/stream.

Subscription registry + compute/push for the hybrid model in
docs/superpowers/specs/2026-09-09-ws-indicator-push-design.md:
``bar-close`` mode recomputes on closed bars, ``tick`` mode recomputes on
forming bars throttled to >= 1 s per (instrument, indicator). Compute goes
through ``compute_indicator`` — the same seam as the REST endpoint — so the
two can never drift. History comes from the datalake tail; a datalake miss
degrades to the live bar alone.
"""
from __future__ import annotations

import logging
import time
from datetime import datetime, timedelta
from typing import Any

log = logging.getLogger(__name__)

_TAIL_LIMIT = 400
_TICK_THROTTLE_SECONDS = 1.0
_POINTS_PER_PUSH = 2
_IST = "Asia/Kolkata"


def _IST_ZONE():
    from zoneinfo import ZoneInfo

    return ZoneInfo(_IST)


def _tf_seconds(tf: Any) -> int:
    from tradex_trading.runtime.bar_aggregator import _tf_seconds as fn

    return fn(tf)


def _candles_from_bars(bars: list[dict[str, Any]]) -> list[Any]:
    """Serialized bars -> candle shims for compute_indicator.

    The compute functions duck-type ``c.ohlc.{open,high,low,close}.value``
    and ``c.volume.value``; the shims carry IST-naive timestamps because the
    vwap session anchor reads ``c.timestamp.date()``.
    """
    from datetime import timezone

    class _P:
        __slots__ = ("value",)

        def __init__(self, v: float) -> None:
            self.value = v

    class _O:
        __slots__ = ("open", "high", "low", "close")

        def __init__(self, b: dict[str, Any]) -> None:
            self.open = _P(b["open"])
            self.high = _P(b["high"])
            self.low = _P(b["low"])
            self.close = _P(b["close"])

    class _V:
        __slots__ = ("value",)

        def __init__(self, v: float) -> None:
            self.value = v

    class _C:
        __slots__ = ("timestamp", "ohlc", "volume")

        def __init__(self, b: dict[str, Any]) -> None:
            IST = timezone(timedelta(hours=5, minutes=30))
            self.timestamp = datetime.fromtimestamp(b["time"], tz=IST).replace(tzinfo=None)
            self.ohlc = _O(b)
            self.volume = _V(b["volume"])

    return [_C(b) for b in bars]


class IndicatorStreamRegistry:
    """Per-connection indicator subscriptions + bar-frame driven pushes.

    ``push`` is the connection's frame sink (same drop-oldest queue bars and
    depth ride). The datalake tail is cached per (instrument, interval) and
    invalidated when the closed bar's bucket advances past the cached head.
    """

    def __init__(self, push: Any) -> None:
        self._push = push
        # (iid, interval, indicator_id) -> {"mode": str, "last_push": float}
        self._subs: dict[tuple[str, str, str], dict[str, Any]] = {}
        # (iid, interval) -> {"through": int, "bars": [...]}  (tail cache)
        self._tails: dict[tuple[str, str], dict[str, Any]] = {}

    # -- subscription management -------------------------------------------

    def add(self, instrument: str, interval: str, ids: list[str], mode: str) -> list[str]:
        """Register ids; returns the accepted list (caller acks)."""
        from tradex_trading.analytics.indicators import get_indicator_spec

        unknown = [i for i in ids if get_indicator_spec(i) is None]
        if unknown:
            raise ValueError(f"unknown indicators: {unknown}")
        if mode not in ("bar-close", "tick"):
            raise ValueError(f"bad mode: {mode!r} (bar-close | tick)")
        for ind_id in ids:
            self._subs[(instrument, interval, ind_id)] = {"mode": mode, "last_push": 0.0}
        return list(ids)

    def remove(self, instrument: str, interval: str, ids: list[str] | None) -> list[str]:
        """Drop ids (None = all for the pair); returns the removed list."""
        targets = [
            key for key in self._subs
            if key[0] == instrument and key[1] == interval and (ids is None or key[2] in ids)
        ]
        removed = [key[2] for key in targets]
        for key in targets:
            del self._subs[key]
        return removed

    def has_subs(self, instrument: str, interval: str) -> bool:
        return any(key[0] == instrument and key[1] == interval for key in self._subs)

    # -- push path -----------------------------------------------------------

    def handle_bar_frame(self, frame: Any) -> None:
        """Called for every bar frame; pushes per subscription mode."""
        key_prefix = (frame.instrument, frame.timeframe)
        targets = [k for k in self._subs if k[:2] == key_prefix]
        if not targets:
            return
        now = time.monotonic()
        for key in targets:
            mode = self._subs[key]["mode"]
            if mode == "bar-close" and not frame.closed:
                continue
            if mode == "tick":
                if frame.closed:
                    continue  # closed bar belongs to bar-close semantics
                last = self._subs[key]["last_push"]
                if now - last < _TICK_THROTTLE_SECONDS:
                    continue
            try:
                self._push_indicator(frame, key[2])
                # Throttle window measured from push completion, so a slow
                # compute cannot immediately admit the next frame.
                self._subs[key]["last_push"] = time.monotonic()
            except Exception:  # noqa: BLE001 — one bad compute never kills the socket
                log.exception("indicator push failed for %s", key)

    def _tail_bars(self, frame: Any) -> list[dict[str, Any]]:
        """Datalake tail for (instrument, timeframe) + the current bar.

        Cache key includes the last closed bucket seen; a new closed bucket
        invalidates. Datalake miss degrades to the live bar alone.
        """
        cache_key = (frame.instrument, frame.timeframe)
        cached = self._tails.get(cache_key)
        if cached is not None and cached["through"] >= frame.time and not frame.closed:
            bars = cached["bars"]
        else:
            bars = self._load_datalake_tail(frame)
            through = frame.time - 1 if frame.closed else frame.time
            self._tails[cache_key] = {"through": through, "bars": bars}
        live = {
            "time": frame.time,
            "open": frame.open,
            "high": frame.high,
            "low": frame.low,
            "close": frame.close,
            "volume": frame.volume,
        }
        if bars and bars[-1]["time"] == frame.time:
            bars = bars[:-1] + [live]
        else:
            bars = bars + [live]
        return bars

    def _load_datalake_tail(self, frame: Any) -> list[dict[str, Any]]:
        try:
            from tradex_brokers.common.market_builders import candles_from_dataframe
            from tradex_domain.enums import Timeframe
            from tradex_domain.instruments import Equity
            from tradex_domain.market import HistoricalSeries

            from tradex_trading.datalake.paths import DATALAKE_ROOT
            from tradex_trading.datalake.parquet_storage import ParquetStorage

            exchange, symbol = frame.instrument.split(":", 1) if ":" in frame.instrument else ("NSE", frame.instrument)
            tf = Timeframe(frame.timeframe)
            end = datetime.fromtimestamp(frame.time, tz=_IST_ZONE()) + timedelta(seconds=_tf_seconds(tf))
            store = ParquetStorage(str(DATALAKE_ROOT))
            df = store.read(symbols=[symbol], start=end - timedelta(days=30), end=end)
            if df.empty:
                return []
            instrument = Equity.of(exchange, symbol)
            series = HistoricalSeries(
                instrument=instrument,
                timeframe=Timeframe.M1,
                candles=candles_from_dataframe(instrument, df, timeframe=Timeframe.M1),
                start=end - timedelta(days=30),
                end=end,
            )
            if tf != Timeframe.M1:
                series = series.resample(tf)
            candles = series.candles[-_TAIL_LIMIT:]
            return [
                {
                    "time": int(c.timestamp.replace(tzinfo=_IST_ZONE()).timestamp()),
                    "open": float(c.ohlc.open.value),
                    "high": float(c.ohlc.high.value),
                    "low": float(c.ohlc.low.value),
                    "close": float(c.ohlc.close.value),
                    "volume": float(c.volume.value),
                }
                for c in candles
            ]
        except Exception:  # noqa: BLE001 — datalake problems degrade to live-only
            log.exception("datalake tail load failed for %s", frame.instrument)
            return []

    def _push_indicator(self, frame: Any, indicator_id: str) -> None:
        from tradex_trading.analytics.indicators import compute_indicator

        bars = self._tail_bars(frame)
        got = compute_indicator(indicator_id, _candles_from_bars(bars))
        plots = list(got.keys())
        points = []
        for i in range(max(0, len(bars) - _POINTS_PER_PUSH), len(bars)):
            point = {"time": bars[i]["time"]}
            for plot in plots:
                value = got[plot][i] if i < len(got[plot]) else None
                point[plot] = value if value is None else float(value)
            points.append(point)
        self._push({
            "type": "indicator",
            "instrument": frame.instrument,
            "interval": frame.timeframe,
            "id": indicator_id,
            "points": points,
        })
