"""HTTP transport abstraction for broker API calls.

Provides a ``Fetch`` protocol that any HTTP callable must satisfy, and a
concrete ``HttpTransport`` implementation using only the standard library.
"""

from __future__ import annotations

import json
import logging
import urllib.error
import urllib.parse
import urllib.request
from collections.abc import Callable, Mapping
from typing import Any, Protocol, runtime_checkable

from tradex_domain import BrokerUnavailableError, ConnectionTimeoutError

log = logging.getLogger(__name__)


@runtime_checkable
class Fetch(Protocol):
    """Minimal callable signature for an HTTP transport."""

    def __call__(
        self,
        method: str,
        url: str,
        *,
        headers: dict | None = None,
        json: Any = None,
        params: dict | None = None,
        timeout: float = 30.0,
        raw_response: bool = False,
    ) -> dict: ...


class HttpTransport:
    """Simple HTTP transport using ``urllib`` (stdlib only, no external deps).

    Parameters
    ----------
    base_url:
        Optional base URL prepended to every *path* passed to ``request()``.
    default_headers:
        Headers sent with every request (e.g. ``Authorization``).
    timeout:
        Default request timeout in seconds.
    """

    def __init__(
        self,
        base_url: str = "",
        default_headers: dict | None = None,
        timeout: float = 30.0,
        *,
        token_provider: Callable[[], str] | None = None,
        auth_headers: Callable[[str], Mapping[str, str]] | None = None,
        on_auth_failure: Callable[[str], None] | None = None,
    ) -> None:
        self._base_url = base_url.rstrip("/")
        self._default_headers: dict[str, str] = dict(default_headers or {})
        self._timeout = timeout
        self._token_provider = token_provider
        self._auth_headers = auth_headers
        self._on_auth_failure = on_auth_failure

    # -- helpers ------------------------------------------------------------

    def _build_url(self, path: str, params: dict | None = None) -> str:
        if path.startswith(("http://", "https://")):
            url = path
        else:
            url = f"{self._base_url}/{path.lstrip('/')}"
        if params:
            qs = urllib.parse.urlencode(params, doseq=True)
            sep = "&" if "?" in url else "?"
            url = f"{url}{sep}{qs}"
        return url

    def _merge_headers(self, extra: dict | None) -> dict[str, str]:
        merged = dict(self._default_headers)
        # Dynamic token injection: call token_provider on every request so
        # mid-session refreshes are picked up automatically (v3 parity).
        if self._token_provider is not None and self._auth_headers is not None:
            token = self._token_provider()
            merged.update(self._auth_headers(token))
        if extra:
            merged.update(extra)
        return merged

    # -- public API ---------------------------------------------------------

    def request(self, method: str, path: str, **kwargs: Any) -> dict:
        """Execute an HTTP request and return the parsed JSON body.

        Parameters
        ----------
        method:
            HTTP method (GET, POST, PUT, DELETE, …).
        path:
            URL path (appended to *base_url*) or a full URL.
        **kwargs:
            Optional *headers*, *json* (body), *params* (query string),
            *timeout*.

        Returns
        -------
        dict
            Parsed JSON response, or ``{"data": <text>}`` for non-JSON bodies.

        Raises
        ------
        BrokerUnavailableError
            On network or HTTP errors.
        """
        url = self._build_url(path, kwargs.get("params"))
        headers = self._merge_headers(kwargs.get("headers"))
        timeout: float = kwargs.get("timeout", self._timeout)

        body: bytes | None = None
        json_payload = kwargs.get("json")
        if json_payload is not None:
            body = json.dumps(json_payload).encode()
            headers.setdefault("Content-Type", "application/json")

        try:
            req = urllib.request.Request(
                url,
                data=body,
                headers=headers,
                method=method.upper(),
            )
            try:
                with urllib.request.urlopen(req, timeout=timeout) as resp:
                    raw = resp.read().decode()
                    try:
                        result = json.loads(raw)
                    except (json.JSONDecodeError, ValueError):
                        result = {"data": raw}
                    result["_http_status"] = resp.status
                    return result
            except urllib.error.HTTPError as exc:
                # Read the error body for diagnostics.  Auth-retry (401/403)
                # is intentionally NOT handled here — that is the exclusive
                # responsibility of ``ProviderHttpClient.request`` so that a
                # stale token burns exactly one mint slot, not two.
                try:
                    err_body = exc.read().decode()
                except Exception:
                    err_body = str(exc)
                raise BrokerUnavailableError(
                    f"HTTP {exc.code} from {method.upper()} {url}: {err_body}"
                ) from exc
        except urllib.error.URLError as exc:
            raise BrokerUnavailableError(
                f"URL error calling {method.upper()} {url}: {exc.reason}"
            ) from exc
        except TimeoutError as exc:
            raise ConnectionTimeoutError(
                f"Request to {method.upper()} {url} timed out after {timeout}s"
            ) from exc
        except (ConnectionError, OSError) as exc:
            raise BrokerUnavailableError(
                f"Transport error calling {method.upper()} {url}: {exc}"
            ) from exc

    # -- convenience methods ------------------------------------------------

    def get(self, path: str, **kwargs: Any) -> dict:
        """Send a GET request."""
        return self.request("GET", path, **kwargs)

    def post(self, path: str, **kwargs: Any) -> dict:
        """Send a POST request."""
        return self.request("POST", path, **kwargs)

    def put(self, path: str, **kwargs: Any) -> dict:
        """Send a PUT request."""
        return self.request("PUT", path, **kwargs)

    def delete(self, path: str, **kwargs: Any) -> dict:
        """Send a DELETE request."""
        return self.request("DELETE", path, **kwargs)

    # Make the transport itself callable as a Fetch
    def __call__(
        self,
        method: str,
        url: str,
        *,
        headers: dict | None = None,
        json: Any = None,
        params: dict | None = None,
        timeout: float = 30.0,
    ) -> dict:
        return self.request(method, url, headers=headers, json=json, params=params, timeout=timeout)


__all__ = [
    "Fetch",
    "HttpTransport",
]
