"""The checked-in goldens are only ground truth if they came from the pinned library.

`trading/tests/analytics/goldens/` drives the parity gates, and every one of them
compares the backend against those files — so if the files were captured from some
other build of openalgo-charts, the whole parity suite is asserting agreement with
a library nobody is running. That is not hypothetical: the generators used to
import the library by an absolute path into one developer's ``~/Downloads``
checkout, which was at 2.1.7 while the repo pins 2.1.8, and nothing said so.

Three silent failure modes, each of which leaves the parity suite green:

* a fixture set regenerated against a different (or stale) library build,
* a fixture or two hand-edited, or regenerated without its companions, and
* a golden set whose declared origin drifts from ``openalgo-charts.pin``.

``PROVENANCE.json`` — written by ``scripts/write-provenance.mjs`` at the end of
``scripts/regenerate-goldens.sh`` — records the answer to each, and this module
fails when any of it stops being true.

Two limits, stated rather than papered over:

* The hash proves the fixtures have not *changed* since the record was written.
  It cannot prove the values in them are the right numbers; only regenerating from
  a pinned, freshly-built library can, which is why the generators refuse to run
  against anything else.
* ``goldens/settings2/`` has no generator in this repository. Its hash is covered
  here, so an edit to it is caught, but it cannot be reproduced from a checkout —
  which is worth knowing before treating it as a snapshot of anything.
"""

from __future__ import annotations

import hashlib
import json
import os
import re
from collections.abc import Iterable
from pathlib import Path
from typing import Any

import pytest

REPO_ROOT = Path(__file__).resolve().parents[3]
GOLDENS = Path(__file__).resolve().parent / "goldens"
PROVENANCE = GOLDENS / "PROVENANCE.json"
PIN_FILE = REPO_ROOT / "openalgo-charts.pin"
# `$OPENALGO_CHARTS_ROOT` unless it is unset or empty — same resolution as
# scripts/library-dist.mjs, so the generator and this gate look at one checkout.
LIBRARY_ROOT = Path(os.environ.get("OPENALGO_CHARTS_ROOT") or REPO_ROOT / "openalgo-charts")

# Not captured *from* the library: the fixture inputs the goldens were computed
# from, and the record itself. Kept in step with `scripts/goldens-provenance.mjs`.
NOT_CAPTURED = frozenset({"PROVENANCE.json", "fixtures.json"})

# Every step `scripts/regenerate-goldens.sh` must have run for the record to
# describe the whole fixture set. Kept in step with `scripts/write-provenance.mjs`.
EXPECTED_GENERATORS = (
    "generate_fixtures.py",
    "generate_goldens.mjs",
    "generate_goldens_transforms.mjs",
    "generate_goldens_profiles.mjs",
)


def walk_files(root: Path) -> list[str]:
    """Every regular file under ``root``, ``/``-separated and sorted.

    Directory symlinks are not followed: a self-referential one would recurse
    forever, and the JS side (`walkFiles`) skips them for the same reason.
    """
    out: list[str] = []
    for path in sorted(root.rglob("*")):
        if path.is_dir() and not path.is_symlink():
            continue
        if path.is_file():
            out.append(path.relative_to(root).as_posix())
    return sorted(out)


def hash_tree(root: Path, exclude: Iterable[str] = ()) -> str:
    """``sha256:<hex>`` over a file tree's *contents*.

    Re-implements ``scripts/hash-tree.mjs``, which documents the algorithm and
    must produce the same hex: for each file in sorted relative-path order, the
    relative path, a NUL, the bytes, a NUL.

    Contents and not timestamps: a ``git checkout`` rewrites the mtime of files it
    did not change, so a timestamp check would call an unchanged fixture set stale.
    """
    skip = set(exclude)
    digest = hashlib.sha256()
    for rel in walk_files(root):
        if rel in skip:
            continue
        digest.update(rel.encode("utf-8"))
        digest.update(b"\0")
        digest.update((root / rel).read_bytes())
        digest.update(b"\0")
    return f"sha256:{digest.hexdigest()}"


def hash_file(path: Path) -> str:
    """``sha256:<hex>`` of one file's bytes, or ``""`` when it does not exist."""
    try:
        return f"sha256:{hashlib.sha256(path.read_bytes()).hexdigest()}"
    except FileNotFoundError:
        return ""


def read_pin() -> str:
    """The commit from ``openalgo-charts.pin``: first non-comment line, first token.

    Mirrors ``scripts/pin.mjs``. A second copy of the pin is a second thing to
    forget to bump, so this reads the one file rather than hardcoding the SHA.
    """
    for line in PIN_FILE.read_text().splitlines():
        stripped = line.strip()
        if stripped == "" or stripped.startswith("#"):
            continue
        sha = stripped.split()[0]
        assert re.fullmatch(r"[0-9a-f]{40}", sha), (
            f"{PIN_FILE}: not a 40-char commit SHA: {sha!r}"
        )
        return sha
    raise AssertionError(f"{PIN_FILE} has no pin in it (every line is blank or a comment)")


def read_provenance() -> dict[str, Any]:
    """The record, failing loudly rather than skipping when it is absent."""
    assert PROVENANCE.exists(), (
        f"{PROVENANCE} is missing, so nothing states where the goldens came from.\n"
        "  Regenerate them from the pinned library: scripts/regenerate-goldens.sh"
    )
    return json.loads(PROVENANCE.read_text())


def verify() -> None:
    """Run every check below, raising on the first failure.

    Imported by ``scripts/regenerate-goldens.sh`` so the pipeline verifies what it
    just wrote through the same gate the suite runs, instead of trusting a write
    that succeeded.
    """
    record = read_provenance()
    _check_pinned(record)
    _check_goldens_hash(record)
    _check_fixtures_hash(record)
    _check_generators(record)


def _check_pinned(record: dict[str, Any]) -> None:
    pin = read_pin()
    assert record.get("pinned") is True, (
        "the record says the goldens were captured from an UNPINNED library "
        f"(recorded sha: {record.get('sha')!r}). Regenerate against the pin:\n"
        "  scripts/regenerate-goldens.sh"
    )
    assert record.get("sha") == pin, (
        f"the goldens were captured from {record.get('sha')}, but openalgo-charts.pin "
        f"is {pin}. Either check the pin out and regenerate, or bump the pin "
        "deliberately — a version bump changes indicator maths, so the diff is the "
        "review."
    )


def _check_goldens_hash(record: dict[str, Any]) -> None:
    actual = hash_tree(GOLDENS, exclude=NOT_CAPTURED)
    assert actual == record.get("goldensSha256"), (
        "the goldens on disk are not the set the record describes "
        f"({actual} != {record.get('goldensSha256')}).\n"
        "  A file was edited, or one generator was re-run without the others.\n"
        "  Regenerate the whole set: scripts/regenerate-goldens.sh"
    )


def _check_fixtures_hash(record: dict[str, Any]) -> None:
    actual = hash_file(GOLDENS / "fixtures.json")
    assert actual == record.get("fixturesSha256"), (
        "fixtures.json is not the one the goldens were computed from "
        f"({actual} != {record.get('fixturesSha256')}). Every golden is a function of "
        "these bars, so they no longer describe the same input set."
    )


def _check_generators(record: dict[str, Any]) -> None:
    listed = record.get("generators") or []
    missing = [g for g in EXPECTED_GENERATORS if g not in listed]
    assert not missing, (
        f"the record does not name every regeneration step (missing: {missing}). "
        "PROVENANCE.json is written by scripts/write-provenance.mjs at the end of a "
        "full run, so a partial one leaves it describing a half-updated directory."
    )


# --- the individual assertions, so a failure names the thing that broke ---------


def test_record_names_the_pinned_library() -> None:
    _check_pinned(read_provenance())


def test_goldens_match_the_recorded_hash() -> None:
    _check_goldens_hash(read_provenance())


def test_fixtures_match_the_recorded_hash() -> None:
    _check_fixtures_hash(read_provenance())


def test_record_names_every_generator_step() -> None:
    _check_generators(read_provenance())


def test_recorded_dist_hash_matches_the_checkout() -> None:
    """When the library checkout is present, the recorded build hash must be real.

    Skipped rather than passed when there is no checkout: this asserts a property
    of *this machine's* library build, and a fresh clone (and most CI jobs) has
    none. The hash chain that matters everywhere — pin, fixtures, goldens — is
    checked by the tests above and needs no library at all.
    """
    dist = LIBRARY_ROOT / "dist"
    if not dist.is_dir():
        pytest.skip(f"no library build at {dist} (regenerate with one to verify the hash)")
    record = read_provenance()
    actual = hash_tree(dist)
    assert actual == record.get("distHash"), (
        "the library build on disk is not the one the goldens were captured from "
        f"({actual} != {record.get('distHash')}).\n"
        f"  cd {LIBRARY_ROOT} && npm run build, then regenerate: scripts/regenerate-goldens.sh"
    )
