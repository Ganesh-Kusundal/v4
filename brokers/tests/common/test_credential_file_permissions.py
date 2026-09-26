"""Credential files must be owner-only (0600) — never world-readable.

The defect this pins: ``_atomic_write_text`` created its temp file with
``temp.open("w")``, which takes the process umask.  With the usual 0022 that is
0644, so every live broker credential written through it (``token_state.json``,
``totp_cooldown.json``, the generation marker) landed world-readable on a shared
machine.

The fix passes ``0o600`` to ``os.open`` at *creation*, so the file is never
readable by group or others even for an instant, and ``os.replace`` carries that
mode onto the destination.

The behavioural tests are skipped where POSIX modes are meaningless (Windows);
the source-level regression guard always runs, since it only reads the file.
"""

from __future__ import annotations

import ast
import json
import os
import re
import stat
import threading
from pathlib import Path

import pytest

from tradex_brokers.common import paths as broker_paths
from tradex_brokers.common import token_lifecycle as tl
from tradex_brokers.common.token_lifecycle import (
    SECRET_FILE_MODE,
    MintTokenManager,
    PortTokenManager,
    _atomic_write_text,
)
from tradex_brokers.common.totp_cooldown import TotpCooldownGuard

_SRC = Path(tl.__file__)
_TOKEN_LIFECYCLE_SRC = _SRC
_PATHS_SRC = Path(broker_paths.__file__)

#: ``os.open``/``io.open``/``Path.open`` — the creations that must carry a
#: mode. Anything else (``handle.open``) is not a path creation.
_CREATION_CALLS = re.compile(r"os\.(?:open|fdopen)\s*\(", re.IGNORECASE)

#: A write whose target is one of the shared secret-state paths (token state,
#: generation marker, TOTP cooldown). Matching on the attribute chain catches
#: ``self._path.write_text(...)`` and ``tmp.write_text(...)`` alike.
_SECRET_TARGET = re.compile(
    r"(_path|_state_path|_generation_path|_token_path|_cooldown_path|tmp|temp)\.write_text",
    re.IGNORECASE,
)

requires_posix_modes = pytest.mark.skipif(
    os.name != "posix",
    reason="POSIX permission bits are not meaningful on this platform",
)


def _mode(path: Path) -> int:
    """Return the permission bits of ``path``."""
    return stat.S_IMODE(os.stat(path).st_mode)


def _no_group_or_other(path: Path) -> bool:
    """Return True when no group/other permission bit is set on ``path``."""
    return not (_mode(path) & (stat.S_IRWXG | stat.S_IRWXO))


# ---------------------------------------------------------------------------
# Behaviour: the file mode
# ---------------------------------------------------------------------------


class TestSecretFileMode:
    """The written file is owner-only, whatever the umask."""

    @requires_posix_modes
    def test_mode_is_exactly_0600(self, tmp_path: Path) -> None:
        path = tmp_path / "token_state.json"
        _atomic_write_text(path, json.dumps({"access_token": "x"}))
        assert _mode(path) == SECRET_FILE_MODE == 0o600

    @requires_posix_modes
    def test_no_group_or_other_bits(self, tmp_path: Path) -> None:
        """The umask must not be able to add group/other bits back."""
        path = tmp_path / "token_state.json"
        _atomic_write_text(path, "{}")
        assert _no_group_or_other(path), f"mode {_mode(path):o} leaks to group/other"

    @requires_posix_modes
    def test_mode_holds_under_a_permissive_umask(self, tmp_path: Path) -> None:
        """A 0 umask is the worst case: 0600 must still hold, not 0666.

        The umask is process-global, so this is restored in a finally block.
        """
        path = tmp_path / "token_state.json"
        previous = os.umask(0o000)
        try:
            _atomic_write_text(path, "{}")
        finally:
            os.umask(previous)
        assert _mode(path) == 0o600
        assert _no_group_or_other(path)

    @requires_posix_modes
    def test_overwrite_tightens_a_loose_destination(self, tmp_path: Path) -> None:
        """A pre-existing 0644 file must come back as 0600 after a write.

        ``os.replace`` moves the temp inode over the destination, so the new
        file's mode wins — which is the whole point of the fix.
        """
        path = tmp_path / "token_state.json"
        path.write_text('{"access_token": "old"}')
        os.chmod(path, 0o644)
        assert _mode(path) == 0o644

        _atomic_write_text(path, '{"access_token": "new"}')

        assert _mode(path) == 0o600
        assert json.loads(path.read_text())["access_token"] == "new"

    @requires_posix_modes
    def test_stale_loose_temp_name_is_replaced_not_reused(self, tmp_path: Path) -> None:
        """A leftover temp file with a loose mode must not survive the write.

        Exercises the ``O_EXCL`` collision path: a crashed writer in the same
        process left a world-readable temp behind.
        """
        path = tmp_path / "token_state.json"
        stale = tmp_path / f".{path.name}.{os.getpid()}.{threading.get_ident()}.tmp"
        stale.write_text("leftover-from-a-crashed-writer")
        os.chmod(stale, 0o666)

        _atomic_write_text(path, '{"access_token": "fresh"}')

        assert not stale.exists(), "stale temp file was reused instead of replaced"
        assert _mode(path) == 0o600
        assert json.loads(path.read_text())["access_token"] == "fresh"


# ---------------------------------------------------------------------------
# Behaviour: the shared writers that produce credential files
# ---------------------------------------------------------------------------


class TestSecretWritersEndToEnd:
    """Both shared paths land 0600, not just the bare helper."""

    @requires_posix_modes
    def test_cooldown_state_path_is_0600(self, tmp_path: Path) -> None:
        """The TOTP cooldown state is written by the same helper."""
        state = tmp_path / "totp_cooldown.json"
        guard = TotpCooldownGuard(cooldown_seconds=120.0, state_path=state)
        guard.record_success()

        assert state.exists()
        assert _mode(state) == 0o600
        assert _no_group_or_other(state)

    @requires_posix_modes
    def test_cooldown_helper_is_the_shared_one(self) -> None:
        """Pin that cooldown really routes through the hardened helper."""
        assert TotpCooldownGuard.__module__.endswith("totp_cooldown")
        assert tl._atomic_write_text is _atomic_write_text

    @requires_posix_modes
    def test_mint_mode_token_state_and_marker_are_0600(self, tmp_path: Path) -> None:
        state = tmp_path / "token_state.json"
        manager = MintTokenManager(state_path=state, mint=lambda: "minted-token")
        manager.ensure_token()

        assert _mode(state) == 0o600
        assert _mode(tmp_path / "token_state.json.generation") == 0o600

    @requires_posix_modes
    def test_port_mode_token_state_is_0600(self, tmp_path: Path) -> None:
        state = tmp_path / "token_state.json"

        class _Port:
            def get_access_token(self) -> str:
                return "t"

            def refresh(self) -> str:
                return "live-token"

            def is_expired(self) -> bool:
                return True

        manager = PortTokenManager(port=_Port(), state_path=state)
        manager.get_token()

        assert _mode(state) == 0o600
        assert json.loads(state.read_text())["access_token"] == "live-token"

    @requires_posix_modes
    def test_new_state_directory_is_owner_only(self, tmp_path: Path, monkeypatch) -> None:
        """A *new* broker state directory is created 0700, not 0755."""
        monkeypatch.setenv("TRADEX_RUNTIME_DIR", str(tmp_path / "runtime"))
        state = broker_paths.default_token_state_path("TESTBROKER")

        directory = state.parent
        assert directory.is_dir()
        assert _mode(directory) == 0o700
        assert not (_mode(directory) & (stat.S_IRWXG | stat.S_IRWXO))

    @requires_posix_modes
    def test_existing_state_directory_is_not_chmodded(
        self, tmp_path: Path, monkeypatch
    ) -> None:
        """Pre-existing directories are left exactly as the user has them.

        The fix is forward-looking in code; it must never silently rewrite the
        permissions of a directory that already exists on disk.
        """
        root = tmp_path / "runtime"
        broker_dir = root / "testbroker"
        broker_dir.mkdir(parents=True)
        os.chmod(broker_dir, 0o755)
        monkeypatch.setenv("TRADEX_RUNTIME_DIR", str(root))

        broker_paths.default_token_state_path("TESTBROKER")

        assert _mode(broker_dir) == 0o755, "an existing directory was chmodded"


# ---------------------------------------------------------------------------
# Behaviour: atomicity is intact
# ---------------------------------------------------------------------------


class TestAtomicityPreserved:
    """The permission fix must not weaken the crash-safety guarantees."""

    @requires_posix_modes
    def test_temp_name_pattern_is_still_used(self, tmp_path: Path) -> None:
        """Writes still stage through the pid+thread temp name, then replace."""
        path = tmp_path / "token_state.json"
        observed: list[str] = []
        real_replace = os.replace

        def _spy(src, dst, *args, **kwargs):  # type: ignore[no-untyped-def]
            observed.append(str(src))
            return real_replace(src, dst, *args, **kwargs)

        tl.os.replace = _spy
        try:
            _atomic_write_text(path, "{}")
        finally:
            tl.os.replace = real_replace

        assert len(observed) == 1
        assert os.path.basename(observed[0]) == (
            f".{path.name}.{os.getpid()}.{threading.get_ident()}.tmp"
        )

    @requires_posix_modes
    def test_destination_is_never_partially_written(self, tmp_path: Path) -> None:
        """A reader watching the real name sees old-or-new, never partial.

        The write is intercepted mid-flight: while the content is being staged
        the real name must still hold the previous content, and once the write
        returns it must hold exactly the new content.
        """
        path = tmp_path / "token_state.json"
        old = json.dumps({"access_token": "old"})
        new = json.dumps({"access_token": "new"})
        _atomic_write_text(path, old)

        seen_during_write: list[str] = []
        real_fdopen = os.fdopen

        class _BlockingFile:
            def __init__(self, wrapped):  # type: ignore[no-untyped-def]
                self._wrapped = wrapped

            def write(self, data):  # type: ignore[no-untyped-def]
                # Mid-write: the real name must not show the new content yet.
                seen_during_write.append(path.read_text())
                return self._wrapped.write(data)

            def __getattr__(self, name):  # type: ignore[no-untyped-def]
                return getattr(self._wrapped, name)

            def __enter__(self):  # type: ignore[no-untyped-def]
                return self

            def __exit__(self, *exc):  # type: ignore[no-untyped-def]
                self._wrapped.close()
                return False

        def _spy_fdopen(*args, **kwargs):  # type: ignore[no-untyped-def]
            return _BlockingFile(real_fdopen(*args, **kwargs))

        tl.os.fdopen = _spy_fdopen
        try:
            _atomic_write_text(path, new)
        finally:
            tl.os.fdopen = real_fdopen

        assert seen_during_write, "the write was never intercepted"
        for snapshot in seen_during_write:
            assert json.loads(snapshot)["access_token"] == "old", (
                "destination observed partially written / replaced too early"
            )
        assert json.loads(path.read_text())["access_token"] == "new"

    def test_failed_write_leaves_previous_content_intact(self, tmp_path: Path) -> None:
        """A crash mid-write must not truncate or wrongly-permission the file."""
        path = tmp_path / "token_state.json"
        _atomic_write_text(path, '{"access_token": "old"}')

        class _Boom(Exception):
            pass

        real_replace = os.replace

        def _explode(*args, **kwargs):  # type: ignore[no-untyped-def]
            raise _Boom("crash between fsync and replace")

        tl.os.replace = _explode
        try:
            with pytest.raises(_Boom):
                _atomic_write_text(path, '{"access_token": "new"}')
        finally:
            tl.os.replace = real_replace

        assert json.loads(path.read_text())["access_token"] == "old"
        # No partial temp file is left visible under the real name.
        leftovers = [p.name for p in tmp_path.iterdir() if p.name != path.name]
        assert leftovers == [], f"temp files left behind: {leftovers}"

    def test_no_temp_file_survives_a_successful_write(self, tmp_path: Path) -> None:
        path = tmp_path / "token_state.json"
        _atomic_write_text(path, "{}")
        _atomic_write_text(path, '{"a": 1}')
        assert sorted(p.name for p in tmp_path.iterdir()) == [path.name]


# ---------------------------------------------------------------------------
# Regression guards: nobody re-introduces a bare, umask-defaulted write
# ---------------------------------------------------------------------------


def _calls_named(tree: ast.AST, attr: str) -> list[ast.Call]:
    """Return every call to ``<anything>.<attr>`` in ``tree``."""
    return [
        node
        for node in ast.walk(tree)
        if isinstance(node, ast.Call)
        and isinstance(node.func, ast.Attribute)
        and node.func.attr == attr
    ]


def _secret_paths() -> list[Path]:
    """Return every broker source that can hold credential material.

    The whole ``tradex_brokers.common`` package is scanned, not just the two
    files this fix touched, so a new module that writes a token or cooldown
    file is covered without anyone remembering to extend this list.
    """
    package = Path(tl.__file__).resolve().parent
    return sorted(p for p in package.glob("*.py") if not p.name.startswith("__"))


class TestNoUmaskDefaultedSecretWrites:
    """Source-level guards. These run everywhere, Windows included."""

    def test_creation_uses_an_explicit_restrictive_mode(self) -> None:
        """Every direct file creation in the package carries a mode argument.

        ``os.open``/``io.open``/``Path.open`` without a ``mode=``/third
        positional argument take the umask, which is how the original defect
        happened.  Note ``mode=`` is the *permission* mode here, distinct from
        the text/binary flag.
        """
        offenders: list[str] = []
        for src in _secret_paths():
            tree = ast.parse(src.read_text(), filename=str(src))
            for call in _calls_named(tree, "open"):
                if not _CREATION_CALLS.search(ast.unparse(call.func)):
                    continue  # e.g. ``handle.open`` — not a path creation
                if len(call.args) >= 3 or len(call.keywords) >= 2:
                    continue  # mode (and flags) supplied
                if not any(
                    kw.arg in {"mode", "opener"} for kw in call.keywords
                ) and len(call.args) < 3:
                    offenders.append(f"{src.name}:{call.lineno} {ast.unparse(call.func)}")
        assert offenders == [], (
            "file creation without an explicit mode takes the umask "
            f"(world-readable at 0022): {offenders}"
        )

    def test_no_plain_text_write_for_credential_files(self) -> None:
        """A ``write_text`` on a credential path is a umask-defaulted write.

        Scoped to writes whose target is (or is derived from) the shared
        secret-state paths, so unrelated non-secret writes in the package are
        not flagged.
        """
        offenders: list[str] = []
        for src in _secret_paths():
            tree = ast.parse(src.read_text(), filename=str(src))
            for call in _calls_named(tree, "write_text"):
                if "opener" in {kw.arg for kw in call.keywords}:
                    continue  # an explicit opener may set the mode
                target = ast.unparse(call.func)
                if not _SECRET_TARGET.search(target):
                    continue
                offenders.append(f"{src.name}:{call.lineno} {target}(...)")
        assert offenders == [], (
            "write_text() creates the file with the umask; route credential "
            f"writes through the hardened atomic writer: {offenders}"
        )

    def test_atomic_write_declares_a_secret_mode_constant(self) -> None:
        source = _TOKEN_LIFECYCLE_SRC.read_text()
        assert "SECRET_FILE_MODE = 0o600" in source, (
            "the 0600 contract must be a named, greppable constant"
        )
        assert "SECRET_DIR_MODE = 0o700" in source

    def test_stale_temp_source_is_not_merely_chmodded_after_open(self) -> None:
        """Guard the ordering: mode must be set at creation, not after."""
        source = ast.unparse(ast.parse(_TOKEN_LIFECYCLE_SRC.read_text()))
        create = re.search(
            r"os\.open\(\s*temp\s*,\s*flags\s*,\s*SECRET_FILE_MODE\s*\)", source
        )
        assert create, "the temp file must be created via os.open(..., SECRET_FILE_MODE)"
        assert "SECRET_FILE_MODE" in create.group(0)


# ---------------------------------------------------------------------------
# Windows / mode-unsupported guard
# ---------------------------------------------------------------------------


def test_secret_mode_constants_are_the_documented_values() -> None:
    """The constants are the contract, on every platform.

    ``os.chmod`` on Windows only toggles the read-only bit, so the numeric
    expectations are pinned here rather than being re-derived per platform.
    """
    assert SECRET_FILE_MODE == 0o600
    assert tl.SECRET_DIR_MODE == 0o700
    assert broker_paths.SECRET_DIR_MODE == 0o700


@requires_posix_modes
def test_created_temp_never_exposes_group_other_even_before_replace(
    tmp_path: Path,
) -> None:
    """The temp file itself is owner-only from the instant it exists.

    A chmod after ``open`` would leave a window; creating with the mode closes
    it.  Observed by inspecting the directory at the moment of the first write.
    """
    path = tmp_path / "token_state.json"
    observed: list[int] = []
    real_fdopen = os.fdopen

    class _Probe:
        def __init__(self, wrapped):  # type: ignore[no-untyped-def]
            self._wrapped = wrapped

        def write(self, data):  # type: ignore[no-untyped-def]
            for entry in tmp_path.iterdir():
                observed.append(stat.S_IMODE(os.stat(entry).st_mode))
            return self._wrapped.write(data)

        def __getattr__(self, name):  # type: ignore[no-untyped-def]
            return getattr(self._wrapped, name)

        def __enter__(self):  # type: ignore[no-untyped-def]
            return self

        def __exit__(self, *exc):  # type: ignore[no-untyped-def]
            self._wrapped.close()
            return False

    tl.os.fdopen = lambda *a, **k: _Probe(real_fdopen(*a, **k))  # type: ignore[assignment]
    try:
        _atomic_write_text(path, "{}")
    finally:
        tl.os.fdopen = real_fdopen  # type: ignore[assignment]

    assert observed, "never observed the temp file mid-write"
    assert all(m == 0o600 for m in observed), f"temp exposed as {[oct(m) for m in observed]}"
