"""Shared fake fetch/transport for broker client tests.

The broker ``from_fetch(fetch=...)`` constructor is the intended test seam
(see ``dhan/client.py`` / ``upstox/client.py``): it composes the real
``HttpTransport`` + ``ProviderHttpClient`` but routes all HTTP through the
injected callable, so a fake never touches the network while exercising the
full auth/cache/resilience path.

``FakeFetch`` records every call and returns scripted responses by URL
(prefix-matched, so tests can stub ``/orders`` and ``/marketfeed/quote``
without knowing the full path).  It supports ``raise_*`` to exercise
transport/error paths.
"""

from __future__ import annotations

from collections.abc import Callable
from typing import Any


class FakeFetch:
    """Scripted ``fetch(method, url, **kwargs) -> dict | (status, dict)``.

    Responses are matched by prefix in order added: the first stub whose
    ``url_prefix`` occurs in the request URL wins.  A ``status`` can be
    provided so the ``_http_status`` marker is injected (auth-retry /
    uncertain-submission classification paths).
    """

    def __init__(self) -> None:
        self.calls: list[dict[str, Any]] = []
        self._stubs: list[tuple[str, dict[str, Any]]] = []
        self._default: dict[str, Any] = {}
        self._ordered: list[dict[str, Any]] | None = None
        self._raise_exc: BaseException | None = None

    def add(
        self,
        url_prefix: str,
        body: dict[str, Any],
        *,
        status: int | None = None,
    ) -> FakeFetch:
        """Register a response for URLs containing *url_prefix*."""
        payload = dict(body)
        if status is not None:
            payload["_http_status"] = status
        self._stubs.append((url_prefix, payload))
        return self

    def default(self, body: dict[str, Any], *, status: int | None = None) -> FakeFetch:
        """Fallback response when no prefix matches."""
        self._default = dict(body)
        if status is not None:
            self._default["_http_status"] = status
        return self

    def ordered(self, bodies: list[dict[str, Any]]) -> FakeFetch:
        """Serve bodies in call order, clamping to the last once exhausted.

        Faithful to the old ``MagicMock`` ``side_effect`` behaviour used by
        ``_make_client``: the i-th call returns ``bodies[i]`` (last repeated).
        """
        self._ordered = [dict(b) for b in bodies]
        return self

    def raise_on(self, exc: BaseException) -> FakeFetch:
        """Make every call raise *exc* (for transport/error paths)."""
        self._raise_exc = exc
        return self

    def __call__(self, method: str, url: str, **kwargs: Any) -> Any:
        self.calls.append({"method": method, "url": url, "kwargs": kwargs})
        if self._raise_exc is not None:
            raise self._raise_exc
        if self._ordered is not None:
            idx = min(len(self.calls) - 1, len(self._ordered) - 1)
            return dict(self._ordered[idx])
        for prefix, body in self._stubs:
            if prefix in url:
                return dict(body)
        return dict(self._default)

    # -- assertion helpers --------------------------------------------------

    def called_with(self, method: str, url_substring: str) -> bool:
        return any(
            c["method"] == method and url_substring in c["url"] for c in self.calls
        )

    def count(self, method: str, url_substring: str) -> int:
        return sum(
            1 for c in self.calls if c["method"] == method and url_substring in c["url"]
        )

    @property
    def request_count(self) -> int:
        return len(self.calls)

    def last_call(self) -> dict[str, Any]:
        """Most recent recorded call: ``{"method", "url", "kwargs"}``."""
        return self.calls[-1]

    def last_payload(self, key: str = "json") -> dict[str, Any]:
        """Most recent call's ``kwargs[key]`` (default ``json``)."""
        return self.calls[-1]["kwargs"].get(key, {})  # type: ignore[no-any-return]


def make_fetch(
    responses: dict[str, dict[str, Any]] | None = None,
    *,
    default: dict[str, Any] | None = None,
    status: int | None = None,
    raise_exc: BaseException | None = None,
) -> FakeFetch:
    """Convenience builder: prefix → body map plus optional defaults."""
    fake = FakeFetch()
    if responses:
        for prefix, body in responses.items():
            fake.add(prefix, body, status=status)
    if default is not None:
        fake.default(default, status=status)
    if raise_exc is not None:
        fake.raise_on(raise_exc)
    return fake


FakeFetchCallable: type = Callable[..., Any]
