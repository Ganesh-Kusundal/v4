"""Tests for extended volume profile: VAH, VAL, LVN."""

from __future__ import annotations

from tradex_trading.analytics.volume.volume_profile import (
    poc, vah, val, lvn, value_area,
)


def _profile() -> dict[float, float]:
    """Sample profile: peak at 104, low-volume nodes at 98 and 110."""
    return {
        96.0: 100, 97.0: 200, 98.0: 30,   # LVN at 98
        99.0: 400, 100.0: 800, 101.0: 1200,
        102.0: 1500, 103.0: 2000, 104.0: 3000,  # POC
        105.0: 2000, 106.0: 1500, 107.0: 1200,
        108.0: 800, 109.0: 400, 110.0: 30,   # LVN at 110
    }


class TestPOC:
    def test_poc_returns_highest_volume_price(self) -> None:
        assert poc(_profile()) == 104.0

    def test_poc_empty_returns_none(self) -> None:
        assert poc({}) is None


class TestValueArea:
    def test_value_area_contains_70_percent(self) -> None:
        """VAH and VAL bound the 70% volume range."""
        profile = _profile()
        lo, hi = value_area(profile)
        total = sum(profile.values())
        contained = sum(v for p, v in profile.items() if lo <= p <= hi)
        assert contained / total >= 0.70

    def test_vah_above_poc(self) -> None:
        profile = _profile()
        assert vah(profile) >= poc(profile)

    def test_val_below_poc(self) -> None:
        profile = _profile()
        assert val(profile) <= poc(profile)

    def test_empty_profile_returns_none(self) -> None:
        assert value_area({}) == (None, None)


class TestLVN:
    def test_lvn_finds_local_minimum(self) -> None:
        """LVN is an interior price level with volume lower than both neighbors."""
        lvns = lvn(_profile())
        # 98.0 is an interior local minimum (30, neighbors are 200 and 400)
        assert 98.0 in lvns
        # 110.0 is at the edge — not an interior LVN
        assert 110.0 not in lvns

    def test_lvn_empty_profile(self) -> None:
        assert lvn({}) == []

    def test_lvn_flat_profile_has_no_lvns(self) -> None:
        flat = {100.0: 500, 101.0: 500, 102.0: 500}
        assert lvn(flat) == []
