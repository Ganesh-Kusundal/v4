"""Compatibility shim — implementation lives in ``tradex_interfaces``."""

from importlib import import_module as _import_module

_impl = _import_module("tradex_interfaces")
globals().update(
    {k: v for k, v in vars(_impl).items() if not k.startswith("__")}
)
if hasattr(_impl, "__all__"):
    __all__ = list(_impl.__all__)
del _import_module, _impl
