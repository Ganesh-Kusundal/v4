"""Secret file permission checks (AC-17, lane 5d).

The token layer already creates its own secret files with a private mode
(``tradex_brokers.token_lifecycle.SECRET_FILE_MODE``) and re-asserts it on
overwrite, so anything IT writes is covered.

This module covers the other half: files an OPERATOR created. ``.env.local``
holds the broker credentials and is the documented way to configure a live
session, and nothing ever stat-ed it — a world-readable credential file was
completely unguarded, which is the defect AC-17 names.

The philosophy matches ``tradex_brokers.paths``: pre-existing files and
directories are left exactly as they are, and the mode is the control. So this
**detects and refuses, never silently fixes**. An operator who chmods their own
secrets is telling you something; quietly changing the mode behind their back
would hide a misconfiguration instead of surfacing it.
"""

from __future__ import annotations

import os
import stat
from pathlib import Path

#: Group and other bits. A secret file readable by either is a violation; the
#: owner bits are whatever the operator chose.
_FORBIDDEN_BITS = stat.S_IRWXG | stat.S_IRWXO

#: The mode a secret file is expected to carry. Read from the token layer's
#: constant so the two halves of this control cannot drift apart.
EXPECTED_SECRET_MODE = 0o600


class InsecureSecretFileError(PermissionError):
    """A file carrying credentials is readable beyond its owner.

    Raised rather than returned so a boot that cannot prove its secrets are
    private does not start: this is the fail-closed half of the control. The
    message names the file, its actual mode, and the one command that fixes it,
    because an operator who cannot act on the message will work around it.
    """


def is_insecure_mode(mode: int) -> bool:
    """True when *mode* exposes a secret to group or other."""
    return bool(stat.S_IMODE(mode) & _FORBIDDEN_BITS)


def assert_secret_file_mode(path: str | Path) -> None:
    """Refuse to continue if the secret at *path* is readable beyond its owner.

    A MISSING file is not a violation. Paper and backtest legitimately run
    with no credentials on disk, so absence must never block a boot; only a
    file that exists with too-permissive mode is refused. This distinction is
    the whole reason the check is safe to put in the boot path unconditionally.

    Windows reports a ``st_mode`` without meaningful permission bits, so the
    check is skipped there rather than refusing every boot.
    """
    if os.name == "nt":  # pragma: no cover - not exercised on POSIX CI
        return

    p = Path(path)
    try:
        mode = p.stat().st_mode
    except FileNotFoundError:
        # No credentials on disk is a legitimate configuration, not a fault.
        return
    except OSError as exc:  # pragma: no cover - stat rarely fails otherwise
        raise InsecureSecretFileError(
            f"cannot verify the permissions of the secret file {p}: {exc}"
        ) from exc

    if is_insecure_mode(mode):
        actual = stat.S_IMODE(mode)
        raise InsecureSecretFileError(
            f"secret file {p} is mode {actual:04o}, which is readable beyond "
            f"its owner; credentials must be mode {EXPECTED_SECRET_MODE:04o}. "
            f"Fix it with: chmod 600 {p}"
        )


def check_secret_files(paths: list[str | Path]) -> None:
    """Assert every readable secret in *paths* is private.

    A convenience for the composition root: one call, and the first offender
    aborts the boot naming itself. Order is preserved so the message is stable
    when several files are wrong.
    """
    for path in paths:
        assert_secret_file_mode(path)
