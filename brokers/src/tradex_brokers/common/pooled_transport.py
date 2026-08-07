"""Connection-pooled HTTP transport for reduced latency."""

from __future__ import annotations

import http.client
import json
import ssl
import threading
from typing import Any
from urllib.parse import urlparse


class PooledHttpTransport:
    """HTTP transport with connection pooling via keep-alive.

    Maintains a pool of persistent HTTPS connections keyed by (host, port).
    Reduces latency from ~100-300ms (new TCP+TLS per request) to ~5-10ms
    (reusing existing connection).
    """

    def __init__(self, pool_size: int = 4, timeout: float = 30.0) -> None:
        self._pool_size = pool_size
        self._timeout = timeout
        self._connections: dict[tuple[str, int], list[http.client.HTTPSConnection]] = {}
        self._lock = threading.Lock()
        self._ctx = ssl.create_default_context()

    def _get_connection(self, host: str, port: int = 443) -> http.client.HTTPSConnection:
        with self._lock:
            pool = self._connections.get((host, port), [])
            while pool:
                conn = pool.pop()
                try:
                    # Test if connection is still alive
                    conn.request("HEAD", "/")
                    return conn
                except Exception:
                    pass  # Connection dead, get a new one
            return http.client.HTTPSConnection(
                host, port, timeout=self._timeout, context=self._ctx
            )

    def _return_connection(
        self, host: str, port: int, conn: http.client.HTTPSConnection
    ) -> None:
        with self._lock:
            key = (host, port)
            pool = self._connections.setdefault(key, [])
            if len(pool) < self._pool_size:
                pool.append(conn)
            else:
                try:
                    conn.close()
                except Exception:
                    pass

    def request(
        self,
        method: str,
        url: str,
        body: Any = None,
        headers: dict[str, str] | None = None,
    ) -> tuple[int, Any]:
        """Send an HTTP request and return (status_code, parsed_body)."""
        parsed = urlparse(url)
        host = parsed.hostname or ""
        port = parsed.port or 443
        path = parsed.path or "/"
        if parsed.query:
            path = f"{path}?{parsed.query}"

        conn = self._get_connection(host, port)
        try:
            hdrs = headers or {}
            hdrs.setdefault("Connection", "keep-alive")
            req_body = json.dumps(body).encode() if body is not None else None
            if req_body is not None:
                hdrs.setdefault("Content-Type", "application/json")
            conn.request(method, path, body=req_body, headers=hdrs)
            resp = conn.getresponse()
            status = resp.status
            raw = resp.read().decode()
            try:
                parsed_body: Any = json.loads(raw)
            except (json.JSONDecodeError, ValueError):
                parsed_body = raw
            self._return_connection(host, port, conn)
            return status, parsed_body
        except Exception:
            try:
                conn.close()
            except Exception:
                pass
            raise

    def close(self) -> None:
        """Close all pooled connections."""
        with self._lock:
            for pool in self._connections.values():
                for conn in pool:
                    try:
                        conn.close()
                    except Exception:
                        pass
            self._connections.clear()


__all__ = ["PooledHttpTransport"]
