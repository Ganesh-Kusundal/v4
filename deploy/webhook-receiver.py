"""Lane 5f — the stand-in for the alert webhook lane 5b's Alertmanager routes at.

``deploy/alertmanager.yml`` (lane 5b) points both receivers at

    http://alertmanager-webhook.tradex.svc.cluster.local:8080/pager-critical
    http://alertmanager-webhook.tradex.svc.cluster.local:8080/ticket

That is deliberate on 5b's part: this repository does not implement a pager and
should not, because the real PagerDuty/Slack credential belongs in a secret the
operator supplies, not in a file a test can read. The honest consequence is that
with nothing behind the URL, an alert that fires evaluates, routes, and is then
delivered nowhere — a stack that looks complete and pages no one.

This module is what makes the route observable instead of imaginary. It is not a
pager and pretends to be neither a pager nor a Slack integration: it is a sink
that records what arrived, so ``docker compose logs alertmanager-webhook`` is
evidence that an alert reached the route. Running the real thing means pointing
``--webhook.url`` at the real thing, or replacing this file, and nothing else
in deploy/ changes.

Deliberately stdlib-only, so it runs on the same ``python:3.12-slim`` base as
the API image with no extra install step and no third-party dependency that
could fail to resolve at deploy time.

Validated by observability/tests/test_deploy_artifacts.py, which reads this
file's bind port out of the source and asserts it is the port the Alertmanager
container is actually told to POST to.
"""

from __future__ import annotations

import json
from http.server import BaseHTTPRequestHandler, HTTPServer
from typing import Any

#: The port lane 5b's ``alertmanager.yml`` receiver URLs already name. Read by
#: the validation test out of the ``BIND_PORT`` literal below, so the two cannot
#: drift: change one and the test fails.
BIND_PORT = 8080

#: The two paths alertmanager.yml posts to. Accepting anything would make a
#: typo in another lane's file look like a delivered alert.
KNOWN_PATHS = ("/pager-critical", "/ticket")


class WebhookHandler(BaseHTTPRequestHandler):
    """Accept an Alertmanager notification, record it, answer 200.

    Answering 200 for a POST is what keeps Alertmanager from retrying: a
    non-2xx makes it redeliver on ``group_interval`` forever, which turns a
    misconfigured route into a log flood rather than a visible failure.
    """

    server_version = "tradex-webhook-sink/1.0"

    def _respond(self, status: int, payload: dict[str, Any]) -> None:
        body = json.dumps(payload).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def do_POST(self) -> None:  # noqa: N802 — BaseHTTPRequestHandler's naming
        """Record the notification. A truncated body is recorded as such."""
        length = int(self.headers.get("Content-Length") or 0)
        raw = self.rfile.read(length) if length else b""
        try:
            body: dict[str, Any] = json.loads(raw or b"{}")
        except ValueError:
            # Not a crash and not a 200: a body that is not JSON means the
            # upstream route is pointed at something that is not an
            # Alertmanager, which is exactly the thing an operator needs to see
            # in the logs rather than have swallowed.
            print(
                f"UNPARSEABLE body of {length}B on {self.path}; "
                "is --webhook.url pointed at the wrong thing?",
                flush=True,
            )
            self._respond(400, {"ok": False, "error": "unparseable body"})
            return

        alerts = body.get("alerts") or []
        names = sorted(
            {str(a.get("labels", {}).get("alertname", "?")) for a in alerts}
        )
        statuses = sorted({str(a.get("status", "?")) for a in alerts})
        # One line per notification, to stdout, which is what `docker compose
        # logs` reads. This is the only evidence the route exists.
        print(
            f"webhook {self.path} "
            f"status={body.get('status', '?')} "
            f"alerts={names} "
            f"alert_statuses={statuses} "
            f"receiver={body.get('receiver', '?')}",
            flush=True,
        )
        self._respond(200, {"ok": True, "received": len(alerts)})

    def do_GET(self) -> None:  # noqa: N802 — BaseHTTPRequestHandler's naming
        """Serve a health path, so compose can tell this container is alive."""
        if self.path in ("/healthz", "/-/healthy"):
            self._respond(200, {"ok": True, "paths": list(KNOWN_PATHS)})
            return
        self._respond(404, {"ok": False, "error": f"no such path: {self.path}"})

    def log_message(self, fmt: str, *args: Any) -> None:
        """Silence the default stderr access log.

        The POST handler already prints the interesting part, and the default
        log would add an unparseable line per request on top of it.
        """


def serve(port: int = BIND_PORT) -> None:
    """Run the sink until killed. Bind all interfaces: the Alertmanager
    container reaches it across the compose network, not over loopback."""
    print(f"tradex webhook sink listening on 0.0.0.0:{port}", flush=True)
    HTTPServer(("0.0.0.0", port), WebhookHandler).serve_forever()


if __name__ == "__main__":
    serve()
