"""Tests for ScannerRuntime."""

from __future__ import annotations

from unittest.mock import MagicMock

from tradex_domain import ScannerDefinition
from tradex_domain.strategy import ScannerResult

from tradex_trading.strategy.scanner_runtime import ScannerRuntime


def _make_definition() -> ScannerDefinition:
    return ScannerDefinition()


def _make_runtime_with_mock_engine() -> tuple[ScannerRuntime, MagicMock]:
    engine = MagicMock()
    engine.run.return_value = []
    runtime = ScannerRuntime(engine)
    return runtime, engine


class TestScannerRuntime:
    def test_add_and_list_definitions(self) -> None:
        runtime, _ = _make_runtime_with_mock_engine()
        d1 = _make_definition()
        d2 = _make_definition()
        runtime.add_definition("momentum", d1)
        runtime.add_definition("mean_reversion", d2)
        assert sorted(runtime.list_definitions()) == ["mean_reversion", "momentum"]

    def test_remove_definition(self) -> None:
        runtime, _ = _make_runtime_with_mock_engine()
        runtime.add_definition("scan_a", _make_definition())
        runtime.add_definition("scan_b", _make_definition())
        runtime.remove_definition("scan_a")
        assert runtime.list_definitions() == ["scan_b"]

    def test_run_all(self) -> None:
        runtime, engine = _make_runtime_with_mock_engine()
        fake_results = [MagicMock(spec=ScannerResult)]
        engine.run.return_value = fake_results
        runtime.add_definition("alpha", _make_definition())
        runtime.add_definition("beta", _make_definition())
        results = runtime.run_all()
        assert set(results.keys()) == {"alpha", "beta"}
        assert results["alpha"] == fake_results
        assert engine.run.call_count == 2

    def test_run_one(self) -> None:
        runtime, engine = _make_runtime_with_mock_engine()
        fake_results = [MagicMock(spec=ScannerResult)]
        engine.run.return_value = fake_results
        runtime.add_definition("gamma", _make_definition())
        result = runtime.run_one("gamma")
        assert result == fake_results
        engine.run.assert_called_once()

    def test_last_results_cached(self) -> None:
        runtime, engine = _make_runtime_with_mock_engine()
        fake = [MagicMock(spec=ScannerResult)]
        engine.run.return_value = fake
        runtime.add_definition("delta", _make_definition())
        assert runtime.last_results() == {}
        runtime.run_all()
        cached = runtime.last_results()
        assert "delta" in cached
        assert cached["delta"] == fake
