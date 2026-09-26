#!/usr/bin/env python3
"""Strangler package extractor for TradeX v4.

Copies a tradex_trading subtree into a new workspace package, rewrites
``tradex_trading.<old>`` imports to ``tradex_<new>``, and replaces the old
tree with thin re-export shims.

Usage:
  .venv/bin/python scripts/extract_package.py \\
      --src-subpath analytics --pkg-name analytics --import-name tradex_analytics
"""

from __future__ import annotations

import argparse
import re
import shutil
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]


def _rewrite_source(text: str, old_prefix: str, new_prefix: str) -> str:
    """Rewrite absolute imports from old_prefix to new_prefix."""
    # from tradex_trading.analytics... import
    text = re.sub(
        rf"from {re.escape(old_prefix)}(\.[\w.]*)? import",
        lambda m: f"from {new_prefix}{m.group(1) or ''} import",
        text,
    )
    # import tradex_trading.analytics...
    text = re.sub(
        rf"import {re.escape(old_prefix)}(\.[\w.]*)?",
        lambda m: f"import {new_prefix}{m.group(1) or ''}",
        text,
    )
    return text


def _shim_for(module_import: str, has_all: bool) -> str:
    body = f'"""Compatibility shim — implementation lives in ``{module_import}``."""\n\n'
    body += f"from {module_import} import *  # noqa: F403\n"
    if has_all:
        body += f"from {module_import} import __all__  # noqa: F401\n"
    return body


def _pyproject(project_name: str, description: str, deps: list[str]) -> str:
    dep_lines = "\n".join(f'    "{d}",' for d in deps)
    deps_block = f"\ndependencies = [\n{dep_lines}\n]\n" if deps else "\ndependencies = []\n"
    pkg = project_name.replace("-", "_")
    return f'''[build-system]
requires = ["hatchling"]
build-backend = "hatchling.build"

[project]
name = "{project_name}"
version = "0.1.0"
description = "{description}"
requires-python = ">=3.12"
{deps_block}
[tool.hatch.build.targets.wheel]
packages = ["src/{pkg}"]

[tool.pytest.ini_options]
testpaths = ["tests"]
addopts = "--import-mode=importlib"
pythonpath = ["src"]
filterwarnings = ["ignore::DeprecationWarning"]
'''


def extract(
    *,
    src_subpath: str,
    pkg_dir_name: str,
    import_name: str,
    project_name: str,
    description: str,
    deps: list[str],
) -> None:
    old_prefix = f"tradex_trading.{src_subpath}"
    src = ROOT / "trading" / "src" / "tradex_trading" / src_subpath
    if not src.is_dir():
        raise SystemExit(f"missing source tree: {src}")

    pkg_root = ROOT / pkg_dir_name
    dst = pkg_root / "src" / import_name
    if dst.exists():
        raise SystemExit(f"destination already exists: {dst}")

    pkg_root.mkdir(parents=True, exist_ok=True)
    (pkg_root / "src").mkdir(exist_ok=True)
    (pkg_root / "tests").mkdir(exist_ok=True)

    shutil.copytree(
        src,
        dst,
        ignore=shutil.ignore_patterns("__pycache__", "*.pyc", ".pytest_cache"),
    )

    # Rewrite imports inside the new package.
    for py in dst.rglob("*.py"):
        text = py.read_text(encoding="utf-8")
        new = _rewrite_source(text, old_prefix, import_name)
        if new != text:
            py.write_text(new, encoding="utf-8")

    # Replace old tree with shims (keep directory layout).
    # First collect relative module paths, then wipe and rewrite.
    rel_files: list[Path] = []
    for py in sorted(src.rglob("*.py")):
        if "__pycache__" in py.parts:
            continue
        rel_files.append(py.relative_to(src))

    shutil.rmtree(src)
    for rel in rel_files:
        target = src / rel
        target.parent.mkdir(parents=True, exist_ok=True)
        mod_parts = rel.with_suffix("").parts
        if mod_parts[-1] == "__init__":
            mod_parts = mod_parts[:-1]
        module_import = ".".join((import_name, *mod_parts)) if mod_parts else import_name
        # Detect __all__ in the new package file.
        new_py = dst / rel
        has_all = "__all__" in new_py.read_text(encoding="utf-8")
        target.write_text(_shim_for(module_import, has_all), encoding="utf-8")

    pyproject = pkg_root / "pyproject.toml"
    if not pyproject.exists():
        pyproject.write_text(
            _pyproject(project_name, description, deps),
            encoding="utf-8",
        )

    # Minimal smoke test
    smoke = pkg_root / "tests" / "test_package_import.py"
    if not smoke.exists():
        smoke.write_text(
            f'"""Smoke: {import_name} is importable."""\n\n'
            f"def test_import_package() -> None:\n"
            f"    import {import_name} as pkg\n"
            f"    assert pkg is not None\n",
            encoding="utf-8",
        )

    print(f"extracted {src_subpath} -> {pkg_dir_name}/src/{import_name} ({len(rel_files)} shims)")


def main() -> None:
    p = argparse.ArgumentParser()
    p.add_argument("--src-subpath", required=True)
    p.add_argument("--pkg-name", required=True, help="directory name under repo root")
    p.add_argument("--import-name", required=True)
    p.add_argument("--project-name", required=True)
    p.add_argument("--description", default="TradeX extracted package")
    p.add_argument("--dep", action="append", default=[], help="project dependency entry")
    args = p.parse_args()
    extract(
        src_subpath=args.src_subpath,
        pkg_dir_name=args.pkg_name,
        import_name=args.import_name,
        project_name=args.project_name,
        description=args.description,
        deps=args.dep,
    )


if __name__ == "__main__":
    main()
