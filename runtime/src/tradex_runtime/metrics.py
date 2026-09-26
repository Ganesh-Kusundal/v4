"""Compatibility shim — implementation lives in ``tradex_observability.metrics``.

The public name here is ``MetricsRegistry`` (imported by
``tradex_interfaces.fastapi_app`` and re-exported from ``tradex_runtime/__init__``).
It used to be forwarded dynamically, via::

    globals().update({k: v for k, v in vars(_impl).items() if not k.startswith("__")})

which mypy cannot follow, so every consumer reported a false
``Module "tradex_runtime.metrics" has no attribute "MetricsRegistry"``
``[attr-defined]``. The explicit re-export below is visible to both.

The dynamic copy also leaked the implementation module's own imports (``re``,
``threading``) and its private helpers (``_Counter``/``_Gauge``/``_Histogram``)
into this module's namespace. Those were accidents of the copy, not part of the
surface: nothing imports them from here, and ``__all__`` — which the dynamic
shim did not define at all, since the target has no ``__all__`` — now states the
real contract.
"""

from tradex_observability.metrics import MetricsRegistry

__all__ = ["MetricsRegistry"]
