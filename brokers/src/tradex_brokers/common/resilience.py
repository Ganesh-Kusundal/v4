"""Combined resilience pipeline: rate-limit → retry → circuit-breaker.

Composes the three primitives into a single ``send()`` call so that broker
adapters do not need to wire them manually.
"""

from __future__ import annotations

import logging
from typing import Any

from tradex_domain import RateLimitError

from tradex_brokers.common.circuit_breaker import CircuitBreaker
from tradex_brokers.common.rate_limit import TokenBucketRateLimiter
from tradex_brokers.common.retry import RetryableHttpClient

log = logging.getLogger(__name__)


class ResiliencePipeline:
    """Composes rate_limit → retry → circuit_breaker into a single send() call.

    The pipeline is applied in the following order:

    1. **Rate limiter** — wait for a token before issuing the call.
    2. **Circuit breaker** — fail fast if the downstream is tripped.
    3. **Retry** — transparently retry transient failures with backoff.

    Parameters
    ----------
    rate_limiter:
        Token-bucket limiter that gates outbound call rate.
    retry:
        HTTP client with built-in retry logic.
    breaker:
        Circuit breaker that short-circuits calls when unhealthy.
    rate_limit_timeout:
        Maximum time (seconds) to wait for a rate-limit token before giving
        up.  Separated from the HTTP ``timeout`` (forwarded to the underlying
        transport) so a slow rate-limit gate does not inflate or mask the
        network timeout.
    """

    def __init__(
        self,
        rate_limiter: TokenBucketRateLimiter,
        retry: RetryableHttpClient,
        breaker: CircuitBreaker,
        *,
        rate_limit_timeout: float | None = None,
    ) -> None:
        self._rate_limiter = rate_limiter
        self._retry = retry
        self._breaker = breaker
        self._rate_limit_timeout = rate_limit_timeout

    # -- public API ---------------------------------------------------------

    def send(self, method: str, url: str, **kwargs: Any) -> Any:
        """Send an HTTP request through the full resilience pipeline.

        Parameters
        ----------
        method:
            HTTP method (GET, POST, PUT, DELETE, …).
        url:
            Target URL.
        **kwargs:
            Forwarded to the underlying retry client (headers, json, params,
            timeout).  An explicit ``rate_limit_timeout`` kwarg, if present,
            overrides the pipeline-level default for this call.

        Returns
        -------
        dict
            Parsed JSON response body.

        Raises
        ------
        RateLimitError
            If the rate limiter cannot provide a token within the timeout.
        BrokerUnavailableError
            If the circuit breaker is OPEN or all retries are exhausted.
        """
        # 1. Rate-limit gate — uses the dedicated rate_limit_timeout, NOT the
        # HTTP transport timeout, so the two concerns stay independent.
        rl_timeout = self._rate_limit_timeout
        if rl_timeout is None:
            rl_timeout = kwargs.get("rate_limit_timeout", 30.0)
        # Strip rate_limit_timeout from kwargs so it is not forwarded to the
        # underlying HTTP client (which only understands transport timeouts).
        kwargs.pop("rate_limit_timeout", None)
        try:
            self._rate_limiter.acquire(timeout=rl_timeout)
        except TimeoutError as exc:
            raise RateLimitError(
                f"Rate limiter could not provide a token within {rl_timeout}s for {url}"
            ) from exc

        # 2+3. Circuit breaker wraps the retry client call
        def _call() -> Any:
            return self._retry.send(method, url, **kwargs)

        return self._breaker.request(_call)


__all__ = [
    "ResiliencePipeline",
]
