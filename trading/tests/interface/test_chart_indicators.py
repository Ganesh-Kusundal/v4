"""Indicator compute endpoint tests — catalogue serving, alignment, nulls."""

from __future__ import annotations

import pytest

fastapi = pytest.importorskip("fastapi")

from fastapi.testclient import TestClient  # noqa: E402
from tradex_interfaces.fastapi_app import create_app  # noqa: E402


def _client() -> TestClient:
    return TestClient(create_app(session=None))


class TestCatalogueEndpoint:
    def test_catalogue_served(self):
        resp = _client().get("/api/charts/indicators")
        assert resp.status_code == 200
        entries = resp.json()["indicators"]
        ids = {e["id"] for e in entries}
        assert {"sma", "ema", "rsi", "macd", "bollinger"} <= ids
        sma_entry = next(e for e in entries if e["id"] == "sma")
        assert sma_entry["placement"] == "overlay"
        assert sma_entry["params"][0]["default"] == 20


@pytest.mark.usefixtures("datalake_or_skip")
class TestComputeEndpoint:
    @pytest.fixture(autouse=True)
    def datalake_or_skip(self):
        from tradex_market_data.parquet_storage import ParquetStorage

        store = ParquetStorage("data/")
        if not store.symbols():
            pytest.skip("empty datalake")

    def _compute(self, body: dict):
        return _client().post("/api/charts/indicators/compute", json=body)

    def test_compute_aligns_points_to_bars(self):
        """Points must be index-aligned with /history bars over the same window."""
        client = _client()
        # 2026-07-06 00:00 UTC .. 2026-07-20 00:00 UTC — inside RELIANCE's
        # datalake coverage (2026-05-05 .. 2026-08-21 IST).
        win = {"exchange": "NSE", "symbol": "RELIANCE", "interval": "5m",
               "from": 1783372800, "to": 1784496000}
        hist = client.get("/api/charts/history/NSE:RELIANCE", params={
            "interval": "5m", "from": win["from"], "to": win["to"]}
        )
        if not hist.json()["bars"]:
            pytest.skip("window has no data")
        bar_times = [b["time"] for b in hist.json()["bars"]]
        resp = self._compute({**win, "id": "sma"})
        assert resp.status_code == 200
        points = resp.json()["points"]
        assert len(points) == len(bar_times)
        assert [p["time"] for p in points] == bar_times

    def test_warmup_is_null_not_nan(self):
        """Warmup region serializes as null — a bare NaN would break JSON."""
        resp = self._compute({
            "exchange": "NSE", "symbol": "RELIANCE", "interval": "5m",
            "id": "sma", "params": {"period": 200},
        })
        assert resp.status_code == 200
        points = resp.json()["points"]
        if not points:
            pytest.skip("no data")
        # With period 200 there must be leading nulls on any realistic window.
        assert points[0]["value"] is None

    def test_unknown_indicator_is_422(self):
        resp = self._compute({
            "exchange": "NSE", "symbol": "RELIANCE", "interval": "5m",
            "id": "does-not-exist",
        })
        assert resp.status_code == 422
        assert "unknown indicator" in resp.json()["error"]["message"]

    def test_unknown_param_is_422(self):
        resp = self._compute({
            "exchange": "NSE", "symbol": "RELIANCE", "interval": "5m",
            "id": "sma", "params": {"bogus": 1},
        })
        assert resp.status_code == 422

    def test_unsupported_interval_is_422(self):
        resp = self._compute({
            "exchange": "NSE", "symbol": "RELIANCE", "interval": "M", "id": "sma",
        })
        assert resp.status_code == 422

    def test_overlay_and_pane_indicators_both_computable(self):
        for indicator_id, params in (
            ("ema", {"period": 9}),
            ("rsi", {}),
            ("bollinger", {}),
            ("vwap", {}),
        ):
            resp = self._compute({
                "exchange": "NSE", "symbol": "RELIANCE", "interval": "15m",
                "id": indicator_id, "params": params,
            })
            assert resp.status_code == 200, (indicator_id, resp.text)


class TestComputeValidatesBeforeReadingTheLake:
    """A malformed request is malformed whatever bars the window holds.

    The default window (``from``/``to`` absent -> "now minus lookback")
    routinely covers no data at all, so the route used to answer 200 with
    ``points: []`` before ``compute_indicator`` ever ran — and the 422 it
    documents was unreachable for the default request shape. These tests
    pin the *ordering*, not the coverage: the windows below are empty by
    construction, so the lake read returns nothing in every environment.
    """

    #: Deliberately outside any datalake coverage: 1998 predates the lake.
    _EMPTY_WINDOW = {"from": 883612800, "to": 884688000}

    def _compute(self, body: dict):
        return _client().post("/api/charts/indicators/compute", json=body)

    def _empty_window_body(self, **overrides) -> dict:
        return {
            "exchange": "NSE", "symbol": "RELIANCE", "interval": "5m",
            **self._EMPTY_WINDOW, **overrides,
        }

    def test_the_window_really_is_empty(self):
        """Guard the guard: if this ever returns bars, the tests below stop
        proving anything and this is the first thing that broke."""
        resp = _client().get("/api/charts/history/NSE:RELIANCE", params={
            "interval": "5m", **self._EMPTY_WINDOW,
        })
        assert resp.json()["bars"] == []
        assert resp.json()["source"] == "none"

    def test_unknown_indicator_is_422_over_an_empty_window(self):
        resp = self._compute(self._empty_window_body(id="does-not-exist"))
        assert resp.status_code == 422, resp.text
        assert "unknown indicator" in resp.json()["error"]["message"]

    def test_unknown_param_is_422_over_an_empty_window(self):
        resp = self._compute(self._empty_window_body(id="sma", params={"bogus": 1}))
        assert resp.status_code == 422, resp.text

    def test_a_valid_indicator_over_an_empty_window_is_still_200(self):
        """The fix must not turn "no data" into an error."""
        resp = self._compute(self._empty_window_body(id="sma"))
        assert resp.status_code == 200, resp.text
        assert resp.json()["points"] == []
        assert resp.json()["meta"]["source"] == "none"

    def test_the_empty_window_cannot_500_on_an_indicator_that_needs_candles(self):
        """`vortex` indexes ``candles[0]`` and raises IndexError on ``[]``.

        Validating without computing is what keeps an empty window a 200 for
        every registered indicator, rather than a 500 for the handful that
        cannot compute over zero bars.
        """
        resp = self._compute(self._empty_window_body(id="vortex"))
        assert resp.status_code == 200, resp.text
        assert resp.json()["points"] == []
