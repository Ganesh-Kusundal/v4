"""Single source of truth for the allowed browser origin.

Two consumers must agree on exactly one origin string: the CORS middleware
(``create_app`` in :mod:`tradex_interfaces.fastapi_app`) and the CSRF guard
(``require_csrf_if_session`` in :mod:`tradex_interfaces.auth.deps`). When they
disagree the API degrades to two half-policies:

  * CORS allows a browser origin the CSRF guard does not trust, so the
    preflight succeeds for an origin that is not the real UI. Nothing
    sensitive leaks (the CSRF guard still rejects), but the browser spends a
    round trip and the operator gets a misleading "CORS is configured" signal.
  * CORS refuses the origin the CSRF guard trusts, so the real UI is blocked
    outright.

Precedence (highest first):

  1. ``state.ui_origin`` — set on ``app.state`` in :func:`create_app`, and
     overridable programmatically by a caller (tests, embedders, a future
     per-tenant config). This is the CSRF guard's historical behaviour,
     preserved exactly.
  2. ``TRADEX_UI_ORIGIN`` — the process environment, read at resolve time.
  3. :data:`DEFAULT_UI_ORIGIN` — the Vite dev-server origin.

Non-strings (including ``None``) and the empty string are treated as "not
configured" at every level, matching the ``getattr(...) or
os.environ.get(...) or DEFAULT`` chain the CSRF guard has always used.
Resolving per request is therefore never more permissive than the pre-existing
CSRF check: for any state/environment pair, :func:`resolve_ui_origin` returns
the same value that inline chain did.

``"*"`` is additionally refused (a warning is logged, and resolution falls
through to the next level). Starlette reads ``allow_origins=["*"]`` as "allow
every origin", so a wildcard reaching CORS would silently disable the origin
lock this module exists to enforce. Refusing it makes the policy strictly
tighter than before for that input, never looser, and keeps both consumers on
one concrete origin.

``app.state.ui_origin`` is deliberately NOT read at ``add_middleware`` time.
Starlette captures the allow-list when it builds the middleware stack (on the
first request) and ``add_middleware`` is not thread-safe once the app is
serving, so resolving any earlier would either miss a caller override or race.
The CORS path resolves inside the middleware constructor instead; see
``tradex_interfaces.fastapi_app._StateResolvedCORSMiddleware``.
"""
from __future__ import annotations

import logging
import os
from typing import Final

log = logging.getLogger(__name__)

#: Starlette's ``allow_origins`` treats a bare ``"*"`` as "allow every origin".
#: Accepted here only so it can be rejected — see :func:`_configured`.
_WILDCARD = "*"

#: Attribute on ``app.state`` holding the resolved origin. Read by the CSRF
#: guard in ``auth.deps`` and by the CORS middleware in ``fastapi_app``.
UI_ORIGIN_STATE_ATTR: Final[str] = "ui_origin"

#: Environment variable consulted when ``app.state.ui_origin`` is unset.
UI_ORIGIN_ENV_VAR: Final[str] = "TRADEX_UI_ORIGIN"

#: Vite dev server default. Kept verbatim from the pre-unification policy.
DEFAULT_UI_ORIGIN: Final[str] = "http://localhost:5173"


def _configured(value: object, source: str) -> str | None:
    """Return *value* as a concrete origin string, or None if unusable.

    ``None``/non-str/empty fall through to the next precedence level. ``"*"``
    is rejected on purpose: handed to ``CORSMiddleware`` it would allow every
    origin, which is the fail-open direction.
    """
    if not isinstance(value, str) or not value:
        return None
    if value == _WILDCARD:
        log.warning(
            "ui_origin.wildcard_rejected source=%s; falling back to the next "
            "configured origin instead of allowing every origin",
            source,
        )
        return None
    return value


def resolve_ui_origin(state: object = None) -> str:
    """Single source of truth for the allowed browser origin.

    Precedence: ``state.ui_origin`` -> ``TRADEX_UI_ORIGIN`` -> default.

    Args:
        state: an ``app.state`` object (or anything exposing a ``ui_origin``
            attribute). ``None`` is accepted so callers without a state
            container can still fall back to the environment.

    Returns:
        The origin string, never empty. Never ``"*"``.
    """
    configured = _configured(
        getattr(state, UI_ORIGIN_STATE_ATTR, None), "app.state"
    )
    if configured is not None:
        return configured
    configured = _configured(os.environ.get(UI_ORIGIN_ENV_VAR), UI_ORIGIN_ENV_VAR)
    if configured is not None:
        return configured
    return DEFAULT_UI_ORIGIN


__all__ = [
    "DEFAULT_UI_ORIGIN",
    "UI_ORIGIN_ENV_VAR",
    "UI_ORIGIN_STATE_ATTR",
    "resolve_ui_origin",
]
