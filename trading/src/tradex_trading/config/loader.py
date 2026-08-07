"""YAML and JSON configuration loader.

Loads configuration from YAML or JSON files into AppConfig.
"""

from __future__ import annotations

import json
import os
from pathlib import Path
from typing import Any


def load_yaml(path: str) -> dict[str, Any]:
    """Load YAML config file. Returns empty dict if file not found.

    Parameters
    ----------
    path : str
        Path to the YAML file.

    Returns
    -------
    dict
        Parsed YAML content, or empty dict if file doesn't exist.
    """
    if not os.path.exists(path):
        return {}

    try:
        import yaml  # type: ignore[import-untyped]

        with open(path, encoding="utf-8") as f:
            data = yaml.safe_load(f)
            return data if isinstance(data, dict) else {}
    except ImportError:
        # PyYAML not installed
        return {}
    except Exception:
        # Any other error — return empty dict
        return {}


def load_config(path: str | Path) -> Any:
    """Load a JSON or YAML config file into an AppConfig.

    Supports ``.json``, ``.yaml``, and ``.yml`` extensions.  JSON is parsed
    with stdlib; YAML requires PyYAML (graceful fallback to empty dict).

    Parameters
    ----------
    path : str | Path
        Path to the config file.

    Returns
    -------
    AppConfig
        Validated application configuration.

    Raises
    ------
    FileNotFoundError
        If the config file does not exist.
    ValueError
        If the file contains invalid JSON/YAML or the root is not an object.
    """
    from tradex_trading.config.schema import AppConfig

    config_path = Path(path)
    if not config_path.exists():
        raise FileNotFoundError(f"config file not found: {config_path}")

    suffix = config_path.suffix.lower()
    raw: dict[str, Any]

    if suffix == ".json":
        try:
            parsed = json.loads(config_path.read_text())
        except json.JSONDecodeError as exc:
            raise ValueError(f"invalid JSON in {config_path}: {exc}") from exc
        if not isinstance(parsed, dict):
            raise ValueError(f"config root must be an object: {config_path}")
        raw = parsed
    elif suffix in (".yaml", ".yml"):
        try:
            import yaml  # type: ignore[import-untyped]

            with open(config_path, encoding="utf-8") as f:
                parsed = yaml.safe_load(f)
        except ImportError as exc:
            raise ValueError(
                f"YAML config requires PyYAML: pip install pyyaml ({config_path})"
            ) from exc
        except Exception as exc:
            raise ValueError(f"invalid YAML in {config_path}: {exc}") from exc
        if not isinstance(parsed, dict):
            raise ValueError(f"config root must be an object: {config_path}")
        raw = parsed
    else:
        # Try JSON first, then YAML
        text = config_path.read_text()
        try:
            parsed = json.loads(text)
            if not isinstance(parsed, dict):
                raise ValueError(f"config root must be an object: {config_path}")
            raw = parsed
        except json.JSONDecodeError:
            try:
                import yaml  # type: ignore[import-untyped]

                parsed = yaml.safe_load(text)
                if not isinstance(parsed, dict):
                    raise ValueError(f"config root must be an object: {config_path}")
                raw = parsed
            except ImportError as exc:
                raise ValueError(
                    f"cannot parse {config_path}: not JSON and PyYAML not installed"
                ) from exc
            except Exception as exc:
                raise ValueError(f"invalid config in {config_path}: {exc}") from exc

    return AppConfig.from_dict(raw)


__all__ = ["load_config", "load_yaml"]
