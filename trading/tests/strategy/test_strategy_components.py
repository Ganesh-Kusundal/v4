"""Reusable strategy components (Quant P1)."""

from __future__ import annotations

from tradex_strategy.core.components import LossCooldown, RegimeFilter, TimeExit


def test_loss_cooldown_suppresses_after_loss() -> None:
    cooldown = LossCooldown(max_bars=3)
    assert cooldown.allows() is True
    cooldown.record_fill(-100.0)  # a loss
    assert cooldown.allows() is False
    for _ in range(3):
        cooldown.record_bar()
    assert cooldown.allows() is True  # cooldown expired


def test_loss_cooldown_ignores_wins() -> None:
    cooldown = LossCooldown(max_bars=3)
    cooldown.record_fill(100.0)  # a win
    assert cooldown.allows() is True


def test_time_exit_fires_after_max_bars() -> None:
    exit_rule = TimeExit(max_bars=5)
    exit_rule.record_entry(bar=10)
    assert exit_rule.should_exit(bar=14) is False
    assert exit_rule.should_exit(bar=15) is True


def test_regime_filter_gates_on_supertrend_direction() -> None:
    class _View:
        def indicator(self, name, **params):
            return {"direction": [1]}

    regime = RegimeFilter(_View())
    assert regime.allows(direction=1) is True
    assert regime.allows(direction=-1) is False


def test_regime_filter_unknown_regime_denies() -> None:
    class _View:
        def indicator(self, name, **params):
            return {"direction": [None]}

    regime = RegimeFilter(_View())
    assert regime.allows(direction=1) is False  # no regime => no trade
