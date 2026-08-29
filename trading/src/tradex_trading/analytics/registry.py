"""IndicatorRegistry — first-class, thread-safe registry of indicator specs.

The registry is the single dispatch seam for every indicator computation:
``AnalyticsEngine.indicator_values`` resolves a name through it instead of a
hardcoded ``if/elif`` selector. Indicator modules opt in by decorating their
compute function (or spec) with :func:`register_indicator`, so adding a new
indicator is a single import-time action.
"""

from __future__ import annotations

import threading
from collections.abc import Callable
from dataclasses import dataclass, field
from typing import Any


@dataclass(frozen=True, slots=True)
class ParamSpec:
    """One named parameter — its type tag and default value."""

    type: str  # 'int' | 'float' | 'str' | 'bool'
    default: Any


@dataclass(frozen=True, slots=True)
class IndicatorSpec:
    """One registered indicator: metadata + compute function.

    The compute function takes the source series (a list of close values, or
    any duck-typed list/series the function expects) plus resolved params
    and returns a result in the same shape :class:`IndicatorResult` carries:
    either a list (single-output) or a ``dict[str, list]`` (multi-output).
    """

    name: str
    inputs: tuple[str, ...]
    params: dict[str, ParamSpec]
    outputs: tuple[str, ...]
    compute: Callable[..., Any]


@dataclass(frozen=True, slots=True)
class IndicatorResult:
    """Aligned computation output: one value-list per output key."""

    values: dict[str, list] = field(default_factory=dict)

    def __getitem__(self, key: str) -> list:
        # Convenience: ``result['value']`` reads like a dict without
        # forcing callers to spell out ``.values['value']`` every time.
        return self.values[key]

    def __contains__(self, key: str) -> bool:
        return key in self.values

    def keys(self):
        return self.values.keys()


class IndicatorRegistry:
    """Thread-safe, insertion-ordered map from indicator name to spec.

    Last registration wins on duplicate ids. Discovery is supported through
    :meth:`all` (preserves the order in which indicators were registered,
    which the catalogue/openapi endpoints depend on).
    """

    def __init__(self) -> None:
        self._specs: dict[str, IndicatorSpec] = {}
        self._lock = threading.Lock()

    def register(self, name: str, spec: IndicatorSpec) -> None:
        """Id-or-replace registration; safe to call concurrently."""
        with self._lock:
            self._specs[name] = spec

    def get(self, name: str) -> IndicatorSpec:
        """Return the spec for *name*; raise ``KeyError`` if unknown."""
        # Fast path: read without the lock; the dict swap is atomic in CPython
        # and a stale miss is recovered on retry.
        spec = self._specs.get(name)
        if spec is not None:
            return spec
        with self._lock:
            spec = self._specs.get(name)
            if spec is None:
                raise KeyError(f"unknown indicator: {name!r}")
            return spec

    def all(self) -> tuple[IndicatorSpec, ...]:
        """All registered specs in insertion order — for discovery/openapi."""
        with self._lock:
            return tuple(self._specs.values())

    def compute(
        self, name: str, series: Any, **params: Any
    ) -> IndicatorResult:
        """Resolve *name* and delegate to the registered compute function.

        The compute function is called with ``(series, **params)``. If the
        result is already an :class:`IndicatorResult`, it is returned as-is;
        otherwise it is normalised into one (a list becomes the ``'value'``
        output; a dict becomes a 1:1 mapping of output names to value-lists).
        """
        spec = self.get(name)
        raw = spec.compute(series, **params)
        if isinstance(raw, IndicatorResult):
            return raw
        if isinstance(raw, dict):
            return IndicatorResult(values=dict(raw))
        return IndicatorResult(values={"value": raw})


# Module-level default registry — most callers want a single, process-wide
# instance populated by decorator-based self-registration at import time.
REGISTRY = IndicatorRegistry()


def register_indicator(
    name: str | None = None,
    spec: IndicatorSpec | None = None,
    *,
    inputs: tuple[str, ...] = (),
    params: dict[str, ParamSpec] | None = None,
    outputs: tuple[str, ...] = ("value",),
) -> Any:
    """Decorator that registers a compute function under a name.

    Two forms are supported, matching what existing modules need:

    1. ``@register_indicator("rsi", inputs=("close",), params={...}, outputs=("value",))``
       — the decorator builds the spec from the keyword arguments and binds
       the function as its ``compute`` callable.
    2. ``REGISTRY.register("rsi", spec)`` — direct registration, when the
       module already has a fully-formed :class:`IndicatorSpec` (e.g. the
       legacy ``indicators.py`` uses an existing dataclass with richer
       catalogue fields). See :func:`register_legacy_spec` for the bridge.

    The decorator is idempotent on re-import: re-registering the same name
    replaces the previous binding, which keeps module reloads safe.
    """
    def _decorator(fn: Callable[..., Any]) -> Callable[..., Any]:
        bound_name = name or fn.__name__
        bound_spec = spec or IndicatorSpec(
            name=bound_name,
            inputs=tuple(inputs),
            params=dict(params or {}),
            outputs=tuple(outputs),
            compute=fn,
        )
        REGISTRY.register(bound_name, bound_spec)
        return fn

    return _decorator


def register_legacy_spec(name: str, spec: Any) -> None:
    """Bridge for the legacy catalogue-shaped ``IndicatorSpec`` (the one
    already used by ``indicators.py`` for the chart frontend's menu).

    That legacy spec has ``id``, ``fn``, and a tuple-of-tuples param schema
    rather than the new :class:`IndicatorSpec` dataclass. We map it into the
    new shape so every legacy registration is also reachable through the
    registry's dispatch — without rewriting 90+ modules in this pass.

    If the input is already a new-style :class:`IndicatorSpec`, it is
    registered unchanged.
    """
    if isinstance(spec, IndicatorSpec):
        REGISTRY.register(name, spec)
        return
    # Legacy shape: extract ``fn`` and the param schema.
    fn = getattr(spec, "fn", None)
    legacy_params = getattr(spec, "params", ()) or ()
    params: dict[str, ParamSpec] = {}
    for entry in legacy_params:
        # legacy entry is a (name, type, default) tuple
        if isinstance(entry, tuple) and len(entry) >= 3:
            pname, ptype, pdefault = entry[0], entry[1], entry[2]
        else:
            pname, ptype, pdefault = (
                getattr(entry, "name", str(entry)),
                getattr(entry, "type", "float"),
                getattr(entry, "default", None),
            )
        params[pname] = ParamSpec(type=ptype, default=pdefault)
    if fn is None:
        # No callable — registration is metadata only; skip compute.
        return
    REGISTRY.register(
        name,
        IndicatorSpec(
            name=name,
            inputs=("close",),
            params=params,
            outputs=("value",),
            compute=fn,
        ),
    )


__all__ = [
    "IndicatorRegistry",
    "IndicatorSpec",
    "IndicatorResult",
    "ParamSpec",
    "REGISTRY",
    "register_indicator",
    "register_legacy_spec",
]
