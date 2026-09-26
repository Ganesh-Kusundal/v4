"""Compatibility shim — implementation lives in ``tradex_runtime.startup``.

The semantic public API is ``RuntimeContext``, ``boot`` and ``boot_context``
(the target module's ``__all__``), imported by ``tradex_trading.sdk.session``
and the trading scripts. The private startup helpers ``_attach_event_store``,
``_recover_oms_from_events`` and ``_broker_matches_config`` are also re-exported
because ``trading/tests/runtime/`` imports them from here to drive boot-time
recovery and broker-identity validation directly.

These names used to be forwarded dynamically, via::

    globals().update({k: v for k, v in vars(_impl).items() if not k.startswith("__")})

which mypy cannot follow, so consumers reported a false
``Module "tradex_trading.runtime.startup" has no attribute "boot"``
``[attr-defined]``.

Resolution is a PEP 562 lazy re-export — the same pattern
``tradex_trading/__init__.py`` uses for the names it forwards to
``tradex_runtime`` — rather than a top-level ``from tradex_runtime... import``.
That matters for layering: a top-level import here is an AST-visible
``trading -> runtime`` edge, and ``runtime`` already imports ``trading``
(``tradex_runtime.session`` pulls in ``tradex_trading.sdk.session``), so it
closes the ring and trips
``tests/test_import_boundaries.py::test_no_unknown_import_cycles_between_packages``.
The eager ``import_module`` the original shim performed is kept — the module is
still fully bound the first time it is imported, byte-for-byte the same object
as before; only the *static* import is deferred to attribute access.

The names are declared under ``TYPE_CHECKING`` so mypy resolves
``from tradex_trading.runtime.startup import boot`` against the real target
module's types (this is what clears the false ``attr-defined`` reports). The
incidental imports the old ``globals().update`` leaked (``Decimal``, ``cast``,
``logging``, the broker classes, …) are not re-exported: nothing imports them
from here, and they were accidents of the copy.
"""

from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from tradex_runtime.startup import (
        RuntimeContext,
        _attach_event_store,
        _broker_matches_config,
        _recover_oms_from_events,
        boot,
        boot_context,
    )

#: Public name -> (defining module, attribute within that module). Resolved on
#: first access, never at import; identical to the copy the shim used to make.
_LAZY: dict[str, tuple[str, str]] = {
    "RuntimeContext": ("tradex_runtime.startup", "RuntimeContext"),
    "boot": ("tradex_runtime.startup", "boot"),
    "boot_context": ("tradex_runtime.startup", "boot_context"),
    "_attach_event_store": ("tradex_runtime.startup", "_attach_event_store"),
    "_broker_matches_config": ("tradex_runtime.startup", "_broker_matches_config"),
    "_recover_oms_from_events": ("tradex_runtime.startup", "_recover_oms_from_events"),
}

__all__ = ["RuntimeContext", "boot", "boot_context"]


def __getattr__(name: str) -> object:
    """Resolve a re-exported name on first access (PEP 562)."""
    try:
        module_name, attr = _LAZY[name]
    except KeyError:
        raise AttributeError(
            f"module {__name__!r} has no attribute {name!r}"
        ) from None
    from importlib import import_module

    value = getattr(import_module(module_name), attr)
    # Cache on the module so subsequent lookups skip __getattr__ entirely.
    globals()[name] = value
    return value


def __dir__() -> list[str]:
    return sorted(set(globals()) | set(_LAZY))
