"""Tests for v3-ported engine symbols: RiskCheckResult."""

from __future__ import annotations

from tradex_execution.engine import RiskCheckResult


# ---------------------------------------------------------------------------
# RiskCheckResult
# ---------------------------------------------------------------------------


class TestRiskCheckResult:
    """RiskCheckResult dataclass."""

    def test_approved_default_reason_empty(self) -> None:
        r = RiskCheckResult(approved=True)
        assert r.approved is True
        assert r.reason == ""

    def test_rejected_with_reason(self) -> None:
        r = RiskCheckResult(approved=False, reason="kill switch")
        assert r.approved is False
        assert r.reason == "kill switch"

    def test_frozen(self) -> None:
        r = RiskCheckResult(approved=True)
        try:
            r.approved = False  # type: ignore[misc]
        except AttributeError:
            pass
        else:
            raise AssertionError("RiskCheckResult should be frozen")
