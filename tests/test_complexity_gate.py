"""The complexity gate must not be loosened.

Wave 0 of spec 0002 turned on ruff's C901, PLR0912 and PLR0915 at deliberately
loose thresholds, because 130 existing functions across 52 files exceed a tight
one and clearing that wall is a cleanup project rather than a safety property.
A loose threshold is only defensible because two other things hold it in place:

1. the ratchet, ``scripts/ruff_ratchet.py`` against ``.ruff_baseline``, so the
   error COUNT can never rise; and
2. this test, so the THRESHOLDS themselves can never rise.

Without the second, the first is not enough: a threshold of 300 lets a function
grow from 50 to 299 complexity without tripping anything, and the count stays at
zero the whole time. So the number a future reader would assume is meaningful is
the number most likely to rot.

If you genuinely need a higher threshold, that is a decision, and it belongs in
``docs/specs/`` with the reason. Do not edit the constants in ``pyproject.toml``:
the change will fail here, which is the entire point.
"""

from __future__ import annotations

import tomllib
from pathlib import Path

import pytest

_ROOT = Path(__file__).resolve().parent.parent
_PYPROJECT = _ROOT / "pyproject.toml"

#: The ceilings spec 0002 lane 0d set, with the worst offender in src/ that
#: forced each one. Raising any of these requires a spec, not a config edit.
EXPECTED_THRESHOLDS: dict[str, int] = {
    "max-complexity": 300,
    "max-branches": 300,
    "max-statements": 850,
}


def _lint_config() -> dict[str, object]:
    data = tomllib.loads(_PYPROJECT.read_text(encoding="utf-8"))
    return data["tool"]["ruff"]["lint"]  # type: ignore[index,return-value]


def test_complexity_rules_are_selected() -> None:
    """The three complexity rules must stay in the select list.

    Removing one makes its threshold dead configuration, which reads as a gate
    and enforces nothing.
    """
    select = _lint_config()["select"]
    for rule in ("C90", "PLR0912", "PLR0915"):
        assert rule in select, f"{rule} was deselected; its threshold is now dead config"


def test_complexity_thresholds_are_not_loosened() -> None:
    """No threshold may rise without a spec. Lowering one is always welcome."""
    config = _lint_config()
    actual: dict[str, int] = {
        **config["mccabe"],  # type: ignore[dict-item]
        **config["pylint"],  # type: ignore[dict-item]
    }
    for name, ceiling in EXPECTED_THRESHOLDS.items():
        assert name in actual, f"{name} was removed from the ruff config"
        current = actual[name]
        assert current <= ceiling, (
            f"{name} was loosened {ceiling} -> {current}. That is a decision, not a "
            f"config edit: write it up in docs/specs/ with the reason, then update "
            f"EXPECTED_THRESHOLDS here deliberately."
        )


def test_tests_stay_exempt_from_complexity_rules() -> None:
    """The per-file-ignores must keep tests/ out of all three rules.

    If this exemption is dropped, the baseline becomes a statement about test
    style rather than about production code, and the count stops being a
    meaningful signal about anything that ships.
    """
    ignores = _lint_config()["per-file-ignores"]
    for pattern in ("**/tests/**", "**/test_*.py"):
        assert pattern in ignores, f"{pattern} is no longer exempt from the complexity rules"
        for rule in ("C901", "PLR0912", "PLR0915"):
            assert rule in ignores[pattern], f"{pattern} no longer ignores {rule}"


def test_ruff_baseline_is_zero() -> None:
    """The lint wall is at zero and must stay there.

    The gate is a count, so a raised baseline is indistinguishable from new debt
    being accepted. scripts/ruff_ratchet.py --set-baseline exists for the
    deliberate case; this asserts the current value so the file and the ratchet
    can never quietly disagree.
    """
    baseline = (_ROOT / ".ruff_baseline").read_text(encoding="utf-8").strip()
    assert baseline == "0", (
        f".ruff_baseline is {baseline}, not 0. If the wall genuinely shrank, "
        f"commit the lower number deliberately; if it grew, that is new debt."
    )


@pytest.mark.parametrize("name", sorted(EXPECTED_THRESHOLDS))
def test_each_threshold_is_documented_with_its_worst_offender(name: str) -> None:
    """Every ceiling must say in pyproject.toml why it is that high.

    A bare number in a config file is the thing that rots. The reason lives
    next to the number, so the next person to raise it has to read the reason
    first.
    """
    text = _PYPROJECT.read_text(encoding="utf-8")
    idx = text.find(f"{name} = ")
    assert idx != -1, f"{name} has no value in pyproject.toml"
    window = text[max(0, idx - 400) : idx]
    assert "worst" in window.lower() or "current" in window.lower(), (
        f"{name} is set without a recorded reason. Every complexity ceiling in this "
        f"file must name the worst offender that forced the value."
    )
