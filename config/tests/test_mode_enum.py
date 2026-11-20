"""``Mode`` is one closed, fail-closed set.

A run mode is a real-money routing decision, so an unknown value (a ``"lvie"``
typo) must raise at config construction rather than fall through the
composition root's branches. ``Mode`` is a ``StrEnum`` so the wire form
(``"live"``) and every existing string comparison keep working.
"""

from __future__ import annotations

from dataclasses import replace

import pytest
from tradex_config.schema import AppConfig
from tradex_domain import Mode
from tradex_domain.constants import MODES


def test_all_modes_parse_from_their_wire_value() -> None:
    for mode in Mode:
        cfg = AppConfig(mode=mode.value)
        assert cfg.mode is mode
        assert cfg.mode == mode.value  # StrEnum: wire-compatible


def test_mode_member_compares_and_serialises_as_its_value() -> None:
    assert Mode.LIVE == "live"
    assert f"{Mode.BACKTEST}" == "backtest"
    assert {m.value for m in Mode} == set(MODES)


def test_default_mode_is_paper() -> None:
    assert AppConfig().mode is Mode.PAPER


def test_unknown_mode_raises_at_construction() -> None:
    with pytest.raises(ValueError, match="unknown mode"):
        AppConfig(mode="lvie")  # typo


def test_unknown_mode_raises_from_from_dict() -> None:
    with pytest.raises(ValueError, match="unknown mode"):
        AppConfig.from_dict({"mode": "sandbox"})


def test_replace_coerces_a_string_mode() -> None:
    cfg = replace(AppConfig(), mode="backtest")
    assert cfg.mode is Mode.BACKTEST
