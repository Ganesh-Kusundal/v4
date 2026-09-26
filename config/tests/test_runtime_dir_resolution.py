"""The config and broker layers must resolve TRADEX_RUNTIME_DIR identically.

``config`` read the same environment variable the broker layer read, but
resolved its own fallback: the bare relative ``".tradex_v4"`` where the broker
layer anchored to ``<repo>/runtime``. With the variable unset the two layers
pointed at two *different* directories, so token state, TOTP cooldowns and
instrument caches forked — and because the config default was relative, it
also moved with the process cwd. ``tradex_domain.paths`` is now the single
resolver; these tests pin that both layers stay on it.
"""

from __future__ import annotations

from pathlib import Path

from tradex_brokers.common.paths import default_runtime_dir as broker_default_runtime_dir
from tradex_config.env import from_env
from tradex_config.schema import AppConfig
from tradex_domain.paths import default_runtime_dir as domain_default_runtime_dir

_REPO_ROOT = Path(__file__).resolve().parents[2]  # config/tests -> config -> repo


def _config_sites() -> tuple[str, str, str, str]:
    """Every runtime_dir the config layer can hand out, as strings."""
    return (
        domain_default_runtime_dir().as_posix(),
        AppConfig().runtime_dir,
        AppConfig.from_dict({}).runtime_dir,
        from_env().runtime_dir,
    )


def test_env_var_set_is_used_by_domain_and_config(monkeypatch, tmp_path) -> None:
    """An explicit override wins in both layers."""
    override = tmp_path / "custom-runtime"
    monkeypatch.setenv("TRADEX_RUNTIME_DIR", str(override))

    assert domain_default_runtime_dir() == override
    assert from_env().runtime_dir == str(override)
    assert AppConfig().runtime_dir == str(override)
    assert AppConfig.from_dict({}).runtime_dir == str(override)


def test_unset_resolves_to_the_same_absolute_path_in_both_layers(
    monkeypatch, tmp_path
) -> None:
    """The regression: unset must mean one directory, not two, and not relative."""
    monkeypatch.delenv("TRADEX_RUNTIME_DIR", raising=False)
    monkeypatch.chdir(tmp_path)  # a relative default would resolve under here

    domain, schema, from_dict, from_env_value = _config_sites()

    assert domain == schema == from_dict == from_env_value
    assert domain == broker_default_runtime_dir().as_posix()
    assert domain == (_REPO_ROOT / "runtime").as_posix()
    assert not (tmp_path / ".tradex_v4").exists(), "cwd-relative default reappeared"


def test_default_is_absolute_not_cwd_relative(monkeypatch, tmp_path) -> None:
    monkeypatch.delenv("TRADEX_RUNTIME_DIR", raising=False)
    monkeypatch.chdir(tmp_path)

    for value in _config_sites():
        assert Path(value).is_absolute(), f"runtime_dir {value!r} is not absolute"


def test_config_default_agrees_with_brokers_default(monkeypatch, tmp_path) -> None:
    """The two layers must not drift; this is the split-brain guard itself."""
    monkeypatch.delenv("TRADEX_RUNTIME_DIR", raising=False)
    monkeypatch.setenv("TRADEX_RUNTIME_DIR", str(tmp_path / "both"))
    assert from_env().runtime_dir == broker_default_runtime_dir().as_posix()

    monkeypatch.delenv("TRADEX_RUNTIME_DIR", raising=False)
    assert from_env().runtime_dir == broker_default_runtime_dir().as_posix()
    assert AppConfig().runtime_dir == broker_default_runtime_dir().as_posix()


def test_resolution_is_independent_of_cwd(monkeypatch, tmp_path) -> None:
    """chdir must not move the runtime root — the fork regression test.

    Existence is compared before/after rather than asserted outright: the
    repo's *parent* already holds a stale ``runtime/dhan/`` from launches made
    from that directory before the default was anchored (a fossil of this very
    bug), and a bare "must not exist" check would fail on it forever. What
    matters is that resolving the default from a new cwd creates nothing
    there.
    """
    monkeypatch.delenv("TRADEX_RUNTIME_DIR", raising=False)

    monkeypatch.chdir(_REPO_ROOT)
    before = _config_sites()

    for elsewhere in (tmp_path, Path("/"), _REPO_ROOT.parent):
        forked = [name for name in (".tradex_v4", "runtime") if (elsewhere / name).exists()]
        monkeypatch.chdir(elsewhere)
        assert _config_sites() == before, f"runtime root moved after chdir({elsewhere})"
        after = [
            name
            for name in (".tradex_v4", "runtime")
            if (elsewhere / name).exists()
        ]
        assert after == forked, (
            f"resolving the default from {elsewhere} created "
            f"{sorted(set(after) - set(forked))}; cwd fork reappeared"
        )


def test_env_var_change_is_observed_after_import(monkeypatch, tmp_path) -> None:
    """Resolution is per call, not frozen at import time.

    A module-level constant would be captured on first import and go stale for
    anything that sets the variable later, re-opening the same disagreement
    this module exists to close.
    """
    monkeypatch.delenv("TRADEX_RUNTIME_DIR", raising=False)
    assert from_env().runtime_dir == broker_default_runtime_dir().as_posix()

    monkeypatch.setenv("TRADEX_RUNTIME_DIR", str(tmp_path / "late"))
    assert from_env().runtime_dir == str(tmp_path / "late")
    assert AppConfig().runtime_dir == str(tmp_path / "late")
