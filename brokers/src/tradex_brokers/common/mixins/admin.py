"""Shared admin mixin — provider-neutral cache invalidation (REF-8).

Only ``invalidate_read_cache`` is shared: its body is identical for Dhan and
Upstox. The kill-switch calls stay per-broker (Dhan ``POST/GET /killswitch``
vs Upstox ``PUT/GET /user/kill-switch``), as do the Dhan-only eDIS methods.
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Any, Protocol

if TYPE_CHECKING:
    from tradex_brokers.common.mixins import ProviderFacade

__all__ = ["SharedAdminMixin"]


class SharedAdminMixin(Protocol):
    """Admin members shared verbatim by both provider clients."""

    def invalidate_read_cache(self: ProviderFacade) -> None:
        """Drop cached read responses so a verification probe hits the wire."""
        http: Any = getattr(self, "_http", None)
        if http is None:
            # Adapter mount (``BaseBroker`` subclass): the provider HTTP
            # facade hangs off the composed transport, not on ``self``.
            http = getattr(getattr(self, "_transport", None), "_http", None)
        if http is not None:
            http.invalidate_cache()
