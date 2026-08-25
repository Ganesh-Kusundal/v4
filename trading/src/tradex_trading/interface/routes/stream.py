"""``/ws/stream`` stays in fastapi_app for now.

The WebSocket handler reads four pieces of state from the closure:
``session``, ``feed_registry``, ``normalize_depth``, and ``outbound_max``.
Moving it out of the closure would require either passing them as
parameters to a single 600+ line function (no real win) or introducing
a class (skip — see audit).

Tracked in C3.5 (follow-up after Phase 3).
"""
