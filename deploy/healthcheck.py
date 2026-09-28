"""Lane 5f — the probe block the container's HEALTHCHECK actually runs.

Docker has ONE healthcheck per container, not a probe pair, so the two probes
AC-17 asks for have to live in one command. This module is that command, and it
exists rather than being a `curl` line because the distinction between the two
probes is the whole point:

    /health/live   Liveness. routes/health.py returns a literal
                   HealthResponse(status="ok", check="live"). It never looks at
                   the session, the feed or the broker. It answers exactly one
                   question: is this process still serving HTTP? A liveness
                   probe that consulted session state would restart a process
                   that is healthy and merely has no feed, which converts a
                   market-data outage into a restart loop.

    /health/ready  Readiness. routes/health.py raises 503 unless
                   _helpers.is_ready(session), which requires
                   str(session.state) == "READY" AND (no feed supervisor OR
                   supervisor.ready). It answers "can this process trade right
                   now". A container that is serving but not ready should be
                   marked unhealthy, not restarted, so this is the exit that
                   decides the container's health.

Three things make it better than the obvious `curl -f` one-liner:

1. **It distinguishes the two failures.** Exit 1 means the process is not
   serving; exit 2 means it is serving but not ready. `docker inspect
   .State.Health.Log[].ExitCode` then says which half of the system failed,
   instead of both collapsing into "unhealthy". That is the difference between
   an operator restarting a stuck boot and an operator looking at the feed.

2. **It cannot be fooled by a 200 with a sad body.** urllib raises
   HTTPError for any status >= 400, so the 503 that /health/ready returns for
   a non-READY session is a failure here and not a success. Using Python also
   removes curl from the image, so the probe has no system package to go
   missing.

3. **It is stdlib-only**, so it runs on the same python:3.12-slim base as the
   API with no install step. A probe that fails because a wheel did not
   resolve would report a healthy container as unhealthy.

The probe paths are the routes the app registers, and
observability/tests/test_deploy_artifacts.py reads them back out of
routes/health.py rather than trusting this file: point either probe at a path
the app does not serve and the test fails.
"""

from __future__ import annotations

import argparse
import json
import os
import sys
import urllib.error
import urllib.request
from typing import NamedTuple

#: Where the app listens. 8080 is the `tradex serve` default (cli.py:79), the
#: port the Dockerfile serves and EXPOSEs, the port compose publishes, and the
#: port deploy/prometheus.yml scrapes. All five are asserted to agree by the
#: validation test, so changing this one alone fails the build.
BIND_HOST = "127.0.0.1"
BIND_PORT = 8080

#: The probe path for each kind, in probe order: liveness first, because if the
#: process is not serving there is no point asking whether it is ready.
LIVE_PATH = "/health/live"
READY_PATH = "/health/ready"
PROBE_PATHS = (LIVE_PATH, READY_PATH)

#: Exit codes, and what each one means to whoever reads the container's health
#: log. Distinct rather than a single 1 precisely so the two failures are
#: separable after the fact.
EXIT_OK = 0
EXIT_NOT_SERVING = 1
EXIT_NOT_READY = 2

#: Per-probe timeout. Well inside the healthcheck's own --timeout, so a slow
#: answer is reported as a slow answer rather than a hung process.
PROBE_TIMEOUT_SECONDS = 2.0


class ProbeResult(NamedTuple):
    """One probe's outcome. ``status`` is the HTTP code, or 0 when no answer."""

    path: str
    ok: bool
    status: int
    detail: str


def _endpoint(port: int) -> str:
    return f"http://{BIND_HOST}:{port}{PROBE_PATHS[0].rsplit('/', 1)[0]}"


def probe(path: str, port: int = BIND_PORT, timeout: float = PROBE_TIMEOUT_SECONDS) -> ProbeResult:
    """GET *path* and classify the answer.

    ``ok`` is True only for a 2xx. A connection error is ``status == 0`` with
    the reason in ``detail``; an HTTP error keeps the code the server actually
    returned, because "503 feed not ready" and "500 unhandled" call for
    different responses from a human and must not read the same.
    """
    url = f"http://{BIND_HOST}:{port}{path}"
    request = urllib.request.Request(url, method="GET")
    try:
        with urllib.request.urlopen(request, timeout=timeout) as response:  # noqa: S310 — loopback
            status = response.status
            body = response.read(4096)
    except urllib.error.HTTPError as exc:
        body = exc.read(4096) if hasattr(exc, "read") else b""
        return ProbeResult(path, False, exc.code, _summarise(body) or exc.reason or "http error")
    except (urllib.error.URLError, OSError) as exc:
        return ProbeResult(path, False, 0, f"no answer: {exc}")
    return ProbeResult(path, 200 <= status < 300, status, _summarise(body))


def _summarise(body: bytes) -> str:
    """One short line from a HealthResponse, so the log names the state.

    The body is {"status": ..., "check": ..., "session_state": ...,
    "feed_state": ..., "feed_ready": ...}; the useful half is the state, and a
    readiness failure that says "session not ready: NEW" tells an operator the
    boot did not finish, which is a different problem from a dead process.
    """
    if not body:
        return ""
    try:
        payload = json.loads(body)
    except ValueError:
        return body[:200].decode("utf-8", "replace")
    if not isinstance(payload, dict):
        return str(payload)[:200]
    parts = [f"{k}={v}" for k, v in payload.items() if v is not None]
    return " ".join(parts)[:300]


def _report(result: ProbeResult) -> None:
    verdict = "ok" if result.ok else "FAIL"
    print(
        f"probe {result.path} {verdict} status={result.status} {result.detail}".rstrip(),
        flush=True,
    )


def run_all(port: int = BIND_PORT) -> int:
    """Run both probes in order and return the process exit code.

    Order matters: liveness is asked first so that a process which is not
    serving at all is reported as ``EXIT_NOT_SERVING`` rather than being
    misattributed to readiness, which would send an operator to look at the
    feed when the process is simply gone.
    """
    live = probe(LIVE_PATH, port)
    _report(live)
    if not live.ok:
        return EXIT_NOT_SERVING

    ready = probe(READY_PATH, port)
    _report(ready)
    if not ready.ok:
        return EXIT_NOT_READY

    return EXIT_OK


def main(argv: list[str] | None = None) -> int:
    """CLI entry point for the HEALTHCHECK.

    ``--probe {live,ready,both}`` runs one probe. ``both`` is the default and
    is what the Dockerfile uses; the single-probe modes exist so an operator
    with a platform that really does have separate liveness and readiness
    probes (systemd, or a cluster this repo does not ship) can call one at a
    time and get the same verdict this file produces.
    """
    parser = argparse.ArgumentParser(
        prog="healthcheck",
        description="Probe the TradeX API's liveness and readiness endpoints.",
    )
    parser.add_argument(
        "--probe",
        choices=("both", "live", "ready"),
        default="both",
        help="which probe to run (default: both)",
    )
    # Overridable so the same script can be run against a non-default port
    # without editing it — the image's own HEALTHCHECK relies on the default.
    parser.add_argument(
        "--port",
        type=int,
        default=int(os.environ.get("TRADEX_PROBE_PORT", BIND_PORT)),
        help=f"port to probe (default: {BIND_PORT})",
    )
    args = parser.parse_args(argv)

    if args.probe == "live":
        result = probe(LIVE_PATH, args.port)
        _report(result)
        return EXIT_OK if result.ok else EXIT_NOT_SERVING
    if args.probe == "ready":
        result = probe(READY_PATH, args.port)
        _report(result)
        return EXIT_OK if result.ok else EXIT_NOT_READY
    return run_all(args.port)


if __name__ == "__main__":
    sys.exit(main())
