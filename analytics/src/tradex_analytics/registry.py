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

    ponytail: the container value type is intentionally ``Any`` — production
    stores the full-fidelity catalog ``IndicatorSpec`` (trigrams, plots,
    levels, ``fn``) so the catalogue, ``compute`` and the engine all read one
    place. The typed ``IndicatorSpec``/``ParamSpec`` classes below remain the
    nominal shape for the decorator form and this module's unit tests.
    """

    def __init__(self) -> None:
        self._specs: dict[str, Any] = {}
        self._lock = threading.Lock()

    def register(self, name: str, spec: Any) -> None:
        """Id-or-replace registration; safe to call concurrently."""
        with self._lock:
            self._specs[name] = spec

    def get(self, name: str) -> Any:
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

    def all(self) -> tuple[Any, ...]:
        """All registered specs in insertion order — for discovery/openapi."""
        with self._lock:
            return tuple(self._specs.values())

    def discard(self, name: str) -> None:
        """Remove *name* if present — used by tests to undo registrations."""
        with self._lock:
            self._specs.pop(name, None)

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
       module already has a fully-formed :class:`IndicatorSpec`.

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


