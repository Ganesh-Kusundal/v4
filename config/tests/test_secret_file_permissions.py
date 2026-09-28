"""Secret file permissions at boot (AC-17, lane 5d).

The token layer creates its own credential files privately and re-asserts the
mode on overwrite, so anything it writes was already covered. These tests cover
the other half: the credential file a human creates by hand, which nothing ever
stat-ed. A world-readable ``.env.local`` holding broker credentials was
completely unguarded.

The control is DETECT AND REFUSE, never silently chmod: an operator who
mis-set the mode needs to be told, not corrected behind their back.
"""

from __future__ import annotations

import os
import stat
from pathlib import Path

import pytest
from tradex_config.permissions import (
    InsecureSecretFileError,
    assert_secret_file_mode,
    check_secret_files,
    is_insecure_mode,
)


@pytest.fixture
def secret(tmp_path: Path) -> Path:
    """A credential file with deliberately permissive content, mode 0600."""
    p = tmp_path / ".env.local"
    p.write_text("DHAN_ACCESS_TOKEN=secret\n")
    p.chmod(0o600)
    return p


def _mode(p: Path) -> int:
    return stat.S_IMODE(p.stat().st_mode)


class TestModePredicate:
    """The predicate, isolated from any file."""

    @pytest.mark.parametrize(
        ("mode", "secure"),
        [
            (0o600, True),
            (0o400, True),
            (0o700, True),
            (0o640, False),  # group readable
            (0o604, False),  # other readable
            (0o644, False),  # world readable, the common accident
            (0o666, False),  # world writable
            (0o777, False),
        ],
    )
    def test_only_owner_bits_are_secure(self, mode: int, secure: bool) -> None:
        assert is_insecure_mode(mode) is (not secure)


class TestAssertSecretFileMode:
    """A private file passes; a shared one refuses and says how to fix it."""

    def test_owner_only_file_passes(self, secret: Path) -> None:
        assert_secret_file_mode(secret)  # must not raise

    @pytest.mark.parametrize("mode", [0o644, 0o640, 0o604, 0o666, 0o777])
    def test_shared_file_refuses_naming_the_file_and_the_fix(
        self, secret: Path, mode: int
    ) -> None:
        secret.chmod(mode)
        with pytest.raises(InsecureSecretFileError) as exc:
            assert_secret_file_mode(secret)
        message = str(exc.value)
        assert str(secret) in message, "the refusal must name the file"
        assert f"{mode:04o}" in message, (
            f"the refusal must state the actual mode, got: {message}"
        )
        assert "chmod 600" in message, (
            f"the refusal must tell the operator the fix, got: {message}"
        )

    def test_a_missing_file_is_not_a_violation(self, tmp_path: Path) -> None:
        """Paper and backtest legitimately run with no credentials on disk.

        Absence must never block a boot; only a file that exists and is too
        permissive is a fault. This is why the check is safe to run
        unconditionally in the boot path.
        """
        assert_secret_file_mode(tmp_path / "does-not-exist.env")

    def test_check_secret_files_reports_the_first_offender(
        self, tmp_path: Path
    ) -> None:
        good = tmp_path / "good.env"
        good.write_text("A=1\n")
        good.chmod(0o600)
        bad = tmp_path / "bad.env"
        bad.write_text("B=2\n")
        bad.chmod(0o644)
        with pytest.raises(InsecureSecretFileError, match="bad.env"):
            check_secret_files([good, bad])

    def test_check_secret_files_passes_when_all_are_private(
        self, tmp_path: Path
    ) -> None:
        a, b = tmp_path / "a.env", tmp_path / "b.env"
        for p in (a, b):
            p.write_text("X=1\n")
            p.chmod(0o600)
        check_secret_files([a, b, tmp_path / "absent.env"])


class TestBootWiring:
    """The check must actually RUN at boot, or it is dead code.

    This is the anti-vacuity control. A permission check nobody calls is the
    same failure class as the gap it was written to close.
    """

    def test_boot_refuses_an_insecure_credential_file(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        from tradex_config.schema import ENV_FILE_ENV_VAR
        from tradex_runtime.startup import boot

        from trading.tests.risk_support import app_config

        secret = tmp_path / ".env.local"
        secret.write_text("DHAN_ACCESS_TOKEN=secret\n")
        secret.chmod(0o644)
        monkeypatch.setenv(ENV_FILE_ENV_VAR, str(secret))

        with pytest.raises(InsecureSecretFileError, match="chmod 600"):
            boot(app_config())

    def test_boot_proceeds_when_the_credential_file_is_private(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        from tradex_config.schema import ENV_FILE_ENV_VAR
        from tradex_runtime.startup import boot

        from trading.tests.risk_support import app_config

        secret = tmp_path / ".env.local"
        secret.write_text("DHAN_ACCESS_TOKEN=secret\n")
        secret.chmod(0o600)
        monkeypatch.setenv(ENV_FILE_ENV_VAR, str(secret))

        session = boot(app_config())
        try:
            assert session is not None
        finally:
            session.stop()

    def test_boot_proceeds_with_no_credential_file_at_all(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """The default paper path must keep working: nothing to check."""
        from tradex_config.schema import ENV_FILE_ENV_VAR
        from tradex_runtime.startup import boot

        from trading.tests.risk_support import app_config

        monkeypatch.delenv(ENV_FILE_ENV_VAR, raising=False)
        session = boot(app_config())
        try:
            assert session is not None
        finally:
            session.stop()

    def test_secret_files_reports_the_env_file_and_the_token_dir(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        from tradex_config.schema import ENV_FILE_ENV_VAR, AppConfig

        monkeypatch.setenv(ENV_FILE_ENV_VAR, "/tmp/creds.env")
        paths = AppConfig().secret_files()
        assert Path("/tmp/creds.env") in paths
        assert any(p.name == "tokens" for p in paths), (
            "the broker token directory must be checked too"
        )

    def test_the_check_is_reachable_without_importing_the_token_layer(
        self,
    ) -> None:
        """The mode convention must not depend on importing ``brokers``.

        ``config`` is a leaf that may not import the broker layer, so the
        expected mode is a local constant with a test pinning it to the token
        layer's value rather than a cross-package import.
        """
        from tradex_config.permissions import EXPECTED_SECRET_MODE

        assert EXPECTED_SECRET_MODE == 0o600
        assert os.name != "nt", "these tests assert POSIX mode bits"
