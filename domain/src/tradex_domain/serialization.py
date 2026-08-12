"""Generic domain serialization (D-10).

All v4 domain objects serialize to/from JSON-safe dicts via ``to_dict()`` /
``from_dict()``. The generic helpers handle Decimal, datetime/date, UUID,
enums, nested frozen dataclasses, tuples, lists, and dicts.

Conventions:
- ``Decimal`` -> ``str`` (lossless)
- ``datetime``/``date`` -> ``isoformat()``
- ``UUID`` -> ``str``
- ``StrEnum``/``Enum`` -> ``.value``
- dataclass -> ``{field: to_dict(value)}`` plus a ``__type__`` marker holding
  ``module.QualifiedName`` so ``from_dict()`` can rebuild concrete subclasses
"""

from __future__ import annotations

import functools
import importlib
from dataclasses import fields, is_dataclass
from datetime import date, datetime
from decimal import Decimal
from enum import Enum
from typing import Any, get_args, get_origin, get_type_hints
from uuid import UUID

from tradex_domain.errors import SDKError

_TYPE_KEY = "__type__"
_DOMAIN_PREFIX = "tradex_domain."


class Serializable:
    """Mixin giving any frozen dataclass ``to_dict()``/``from_dict()`` (D-10)."""

    __slots__ = ()

    def to_dict(self) -> dict[str, object]:
        return to_dict(self)

    @classmethod
    def from_dict(cls, data: dict[str, object]) -> Any:
        return from_dict(cls, data)


def to_dict(value: Any) -> Any:
    if value is None or isinstance(value, (str, int, float, bool)):
        return value
    if isinstance(value, Decimal):
        return str(value)
    if isinstance(value, datetime):
        return value.isoformat()
    if isinstance(value, date):
        return value.isoformat()
    if isinstance(value, UUID):
        return str(value)
    if isinstance(value, Enum):
        return value.value
    if isinstance(value, tuple):
        return [to_dict(item) for item in value]
    if isinstance(value, list):
        return [to_dict(item) for item in value]
    if isinstance(value, dict):
        return {str(k): to_dict(v) for k, v in value.items()}
    if is_dataclass(value):
        out: dict[str, object] = {f.name: to_dict(getattr(value, f.name)) for f in fields(value)}
        out[_TYPE_KEY] = f"{type(value).__module__}.{type(value).__qualname__}"
        return out
    raise SDKError(f"cannot serialize {type(value).__name__}")


def _coerce(annotation: Any, value: Any) -> Any:
    if value is None:
        return None
    origin = get_origin(annotation)
    args = get_args(annotation)
    if origin is tuple:
        if args and args[-1] is Ellipsis:
            item_type = args[0]
            return tuple(_coerce(item_type, v) for v in value)
        return tuple(_coerce(t, v) for t, v in zip(args, value, strict=False))
    if origin is list:
        (item_type,) = args if args else (Any,)
        return [_coerce(item_type, v) for v in value]
    if origin is dict:
        key_type, value_type = args if args else (Any, Any)
        return {_coerce(key_type, k): _coerce(value_type, v) for k, v in value.items()}
    if origin is not None:  # Union[...] / Optional[...]
        non_none = [a for a in args if a is not type(None)]
        for arg in non_none:
            try:
                return _coerce(arg, value)
            except (TypeError, ValueError):
                continue
        return value
    if annotation is Any:
        return value
    if annotation is Decimal:
        return Decimal(str(value))
    if annotation is datetime:
        return datetime.fromisoformat(value)
    if annotation is date:
        return date.fromisoformat(value)
    if annotation is UUID:
        return UUID(str(value))
    if isinstance(annotation, type) and issubclass(annotation, Enum):
        return annotation(value)
    if is_dataclass(annotation):
        return from_dict(annotation, value)  # type: ignore[arg-type]
    if annotation in (str, int, float, bool):
        return annotation(value)
    return value


def _resolve_type(marker: str) -> type | None:
    """Resolve a ``module.QualifiedName`` marker back to a class.

    Only ``tradex_domain.``-prefixed markers are accepted.
    """
    if not marker.startswith(_DOMAIN_PREFIX):
        return None
    try:
        module_name, _, qualname = marker.rpartition(".")
        obj: object = importlib.import_module(module_name)
        for part in qualname.split("."):
            obj = getattr(obj, part)
        return obj if isinstance(obj, type) else None
    except (AttributeError, ImportError, ValueError):
        return None


def from_dict(cls: type, data: dict[str, Any]) -> Any:
    """Rebuild a frozen dataclass from a dict produced by :func:`to_dict`.

    If the dict carries a ``__type__`` marker for a concrete subclass of ``cls``
    (e.g. ``Equity`` when ``cls`` is the abstract ``Instrument``), that subclass
    is instantiated instead of the base type.
    """
    if not is_dataclass(cls):
        raise SDKError(f"from_dict requires a dataclass, got {cls.__name__}")
    target: type = cls
    marker = data.get(_TYPE_KEY)
    if isinstance(marker, str):
        resolved = _resolve_type(marker)
        if resolved is not None and is_dataclass(resolved) and issubclass(resolved, cls):
            target = resolved
    kwargs: dict[str, Any] = {}
    for f in fields(target):
        if f.name not in data:
            continue
        kwargs[f.name] = _coerce(_hints(target).get(f.name, f.type), data[f.name])
    return target(**kwargs)


@functools.cache
def _hints(cls: type) -> dict[str, Any]:
    """Cached resolved type hints."""
    return get_type_hints(cls)


__all__ = ["Serializable", "from_dict", "to_dict"]
