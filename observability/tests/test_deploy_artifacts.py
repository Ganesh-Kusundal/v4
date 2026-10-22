"""Lane 5f — the deploy artifacts agree with the app, and the app agrees with itself.

``deploy/Dockerfile``, ``deploy/compose.yml`` and ``deploy/prometheus.yml`` are
YAML nobody checks, which is the condition under which a probe silently points
at a route that was renamed two lanes ago and a container comes up permanently
unhealthy with nothing in CI red. This file is what makes them trustworthy.

Each property below is one way the deployment could be quietly wrong:

  1. **The probe paths are routes the app actually serves.** Read out of
     ``tradex_interfaces.fastapi_app.create_app()``'s OpenAPI schema, not out
     of a constant in this file. A hardcoded list here would only prove the
     list agrees with itself, which is the same defect the check exists to
     prevent. A probe pointed at ``/healthz`` — a path that 404s — is a
     container that never reports healthy, and a probe pointed at ``/health``
     — which always returns 200 — is a probe that can never fail, which the
     task statement is explicit is worse than no probe at all.
  2. **The two probes are not the same endpoint.** ``/health/live`` returns a
     literal and never inspects the session; ``/health/ready`` 503s on a
     non-READY session. Pointing liveness at readiness turns a market-data
     outage into a restart loop, so the two must be distinct AND each must be
     the one whose behaviour matches its name.
  3. **The port agrees in all five places it appears** — the CLI default, the
     Dockerfile EXPOSE, the container CMD, the healthcheck script and the
     Prometheus scrape target. A scrape config on the wrong port is a
     Prometheus that scrapes nothing while every alert stays green, which is
     exactly the "alert on a metric nothing emits" failure
     ``test_alert_rules.py`` was written to catch, one layer up.
  4. **The serving command is the one the repo declares.** ``tradex serve``,
     not a raw ``uvicorn module:factory`` — the factory cannot see its own bind
     host and so sits outside ``_require_live_bind_loopback``.
  5. **The alerting path is closed end to end.** ``prometheus.yml`` points at
     lane 5b's rule file, ``alertmanager.yml`` is mounted where the Alertmanager
     is told to read it, and the receiver URL in that file is a name something
     in ``compose.yml`` actually provides. A receiver pointing at a host no
     service creates is an alert that fires, routes, and is delivered nowhere.
  6. **The probe is real.** ``deploy/healthcheck.py`` is executed against a
     live app for real: a ready session exits 0, a non-READY session exits 2,
     and a dead port exits 1. A probe block that cannot tell those three apart
     is three states collapsed into one, and the test says so.

House style: this mirrors ``observability/tests/test_alert_rules.py`` — the
same stdlib-only YAML subset parser (pyyaml is not a dependency of this
project), the same "read the source rather than trust a constant" discipline,
and the same refusal to accept a config that parses into something useless.
"""

from __future__ import annotations

import re
import socket
import threading
import time
from http.server import BaseHTTPRequestHandler, HTTPServer
from pathlib import Path
from typing import Any

import pytest

REPO_ROOT = Path(__file__).resolve().parents[2]
DEPLOY_DIR = REPO_ROOT / "deploy"

DOCKERFILE_PATH = DEPLOY_DIR / "Dockerfile"
COMPOSE_PATH = DEPLOY_DIR / "compose.yml"
PROMETHEUS_PATH = DEPLOY_DIR / "prometheus.yml"
HEALTHCHECK_PATH = DEPLOY_DIR / "healthcheck.py"
RECEIVER_PATH = DEPLOY_DIR / "webhook-receiver.py"
RULES_PATH = DEPLOY_DIR / "prometheus-rules.yml"
ALERTMANAGER_PATH = DEPLOY_DIR / "alertmanager.yml"

INTERFACES_SRC = REPO_ROOT / "interfaces" / "src"

#: The route the app registers for Prometheus to scrape, and the one lane 5b's
#: rules read the counter names out of. Both are cross-checked against the app.
METRICS_PATH = "/metrics"


# ---------------------------------------------------------------------------
# Minimal YAML (stdlib only — pyyaml is not a dependency of this project)
# ---------------------------------------------------------------------------


def _yaml_scalar(token: str):
    """Coerce one scalar token to bool/int/float/str/None."""
    token = token.strip()
    if len(token) >= 2 and token[0] == token[-1] and token[0] in "\"'":
        return token[1:-1]
    if token in ("true", "True"):
        return True
    if token in ("false", "False"):
        return False
    if token in ("null", "~", ""):
        return None
    if re.fullmatch(r"-?\d+", token):
        return int(token)
    if re.fullmatch(r"-?\d+\.\d+", token):
        return float(token)
    return token


def _is_mapping_key(text: str) -> bool:
    return bool(re.match(r'^([A-Za-z0-9_.\-]+):(\s|$)', text))


def _parse_yaml(text: str):
    """Parse the block-mapping/block-sequence subset used by ``deploy/*.yml``.

    The same parser ``test_alert_rules.py`` uses, and for the same reason: this
    project has no pyyaml dependency, and adding one to read a config file is a
    worse trade than 90 lines of explicit subset parsing that raises on
    anything outside the subset rather than silently misreading it.
    """
    lines: list[tuple[int, str]] = []
    for raw in text.splitlines():
        stripped = raw.strip()
        if not stripped or stripped.startswith("#"):
            continue
        lines.append((len(raw) - len(raw.lstrip(" ")), stripped))

    cursor = [0]

    def parse_block(indent: int):
        if cursor[0] >= len(lines):
            return None
        first = lines[cursor[0]][1]
        return parse_seq(indent) if first == "-" or first.startswith("- ") else parse_map(indent)

    def parse_seq(indent: int):
        out = []
        while cursor[0] < len(lines):
            ind, content = lines[cursor[0]]
            if ind < indent or not (content == "-" or content.startswith("- ")):
                break
            if ind > indent:
                raise ValueError(
                    f"unexpected indent in sequence: {ind} (want {indent}): {content!r}"
                )
            item = content[1:].strip()
            cursor[0] += 1
            if item == "":
                out.append(parse_block(lines[cursor[0]][0]) if cursor[0] < len(lines) else None)
            elif _is_mapping_key(item):
                out.append(_parse_item_mapping(item, indent))
            else:
                out.append(_yaml_scalar(item))
        return out

    def _parse_item_mapping(first_item: str, seq_indent: int) -> dict:
        entry: dict = {}
        key, value = first_item.split(":", 1)
        key, value = key.strip().strip("\"'"), value.strip()
        if value == "":
            entry[key] = _nested_or_none(seq_indent)
        else:
            entry[key] = _yaml_scalar(value)
        while cursor[0] < len(lines):
            ind, content = lines[cursor[0]]
            if ind <= seq_indent or content == "-" or content.startswith("- "):
                break
            sub_key, sub_value = content.split(":", 1)
            sub_key, sub_value = sub_key.strip().strip("\"'"), sub_value.strip()
            cursor[0] += 1
            entry[sub_key] = (
                _nested_or_none(ind) if sub_value == "" else _yaml_scalar(sub_value)
            )
        return entry

    def _nested_or_none(parent_indent: int):
        if cursor[0] < len(lines) and lines[cursor[0]][0] > parent_indent:
            return parse_block(lines[cursor[0]][0])
        return None

    def parse_map(indent: int):
        out = {}
        while cursor[0] < len(lines):
            ind, content = lines[cursor[0]]
            if ind < indent:
                break
            if ind > indent:
                raise ValueError(
                    f"unexpected indent in mapping: {ind} (want {indent}): {content!r}"
                )
            if content == "-" or content.startswith("- "):
                break
            key, value = content.split(":", 1)
            key, value = key.strip().strip("\"'"), value.strip()
            cursor[0] += 1
            out[key] = _nested_or_none(ind) if value == "" else _yaml_scalar(value)
        return out

    return parse_block(lines[0][0] if lines else 0)


# ---------------------------------------------------------------------------
# Reading the artifacts
# ---------------------------------------------------------------------------


def _dockerfile_text() -> str:
    assert DOCKERFILE_PATH.is_file(), f"missing {DOCKERFILE_PATH}"
    return DOCKERFILE_PATH.read_text(encoding="utf-8")


def _dockerfile_instructions() -> str:
    """The Dockerfile with comment lines removed.

    The instruction text is what the test reads, never the prose explaining it.
    Checking ``--workers not in text`` against a file whose own comment says
    "``--workers/--reload`` is REFUSED" would fail for the right reason in the
    wrong way — and a test that a well-written comment can break is a test
    authors will work around instead of satisfy.
    """
    return "\n".join(
        line
        for line in _dockerfile_text().splitlines()
        if not line.lstrip().startswith("#")
    )


def _healthcheck_source() -> str:
    assert HEALTHCHECK_PATH.is_file(), f"missing {HEALTHCHECK_PATH}"
    return HEALTHCHECK_PATH.read_text(encoding="utf-8")


def _port_of(hostport: str) -> int:
    """The port in a ``host:port`` target, or a bare host as port 80.

    Split out because every target in the deploy artifacts is written this way
    and getting the split wrong in one place and not another is how a test
    ends up checking the host half and calling it the port.
    """
    host, separator, port = hostport.rpartition(":")
    if not separator:
        return 80
    assert port.isdigit(), f"target {hostport!r} has a non-numeric port"
    return int(port)


def _host_of(hostport: str) -> str:
    """The host in a ``host:port`` target, or the whole string if bare."""
    host, separator, _ = hostport.rpartition(":")
    return host if separator and host else hostport


def _container_mounts(service: dict) -> dict[str, str]:
    """The container paths a service mounts, mapped to their host source.

    A compose short-syntax volume is ``source:target[:mode]``, and the mode is
    what broke the first version of this: splitting on the first colon gave
    ``/etc/prometheus/prometheus.yml:ro`` as the container path, so a mount
    that was exactly right read as a mismatch. Dropping everything from the
    third segment on is the fix, and it is why the read-only marker on every
    mount in this stack is safe to have.
    """
    mounts: dict[str, str] = {}
    for entry in service.get("volumes") or []:
        parts = str(entry).split(":")
        if len(parts) < 2:
            continue
        source, target = parts[0], parts[1]
        if target.startswith("/"):
            mounts[target] = source
    return mounts


def _compose_aliases() -> dict[str, set[str]]:
    """Every DNS name compose will publish, mapped to the service behind it.

    Three sources, all of which compose publishes and any of which a receiver
    URL could legitimately use:
      * the service name itself
      * every explicit ``aliases`` entry on its networks
      * the service name joined to the network's ``name:`` override

    The third models Docker's ``<service>.<network-name>`` naming.

    A note on that third source, because it was checked rather than assumed:
    Docker's network reference documents ``--network-alias`` and compose's
    ``aliases:`` as the way to add a name, and does not document Docker
    synthesising ``<service>.<network-name>`` on a *user-defined* network. The
    synthesis is a property of Docker's *default* bridge network, where
    Compose gives every service an alias named after the project. Whether it
    holds on a user-defined network was not verified against a live daemon in
    this lane (no registry access to pull a probe image), so it is not relied
    on: :func:`test_alertmanager_delivers_to_a_name_compose_publishes` asserts
    the mechanism that actually delivers instead, and this helper exists so a
    URL spelled any of the three ways is accepted rather than failing on a
    formality.
    """
    compose = _compose_doc()
    services = compose.get("services") or {}
    networks = {
        key: (value or {}).get("name") or key
        for key, value in (compose.get("networks") or {}).items()
    }
    published: dict[str, set[str]] = {}
    for name, service in services.items():
        if not isinstance(service, dict):
            continue
        names = {name}
        # `networks` is either a list of names or a mapping of name to spec,
        # and this file uses both forms across its services.
        declared = service.get("networks") or []
        pairs = declared.items() if isinstance(declared, dict) else [
            (entry, {}) for entry in declared
        ]
        for network_key, spec in pairs:
            if isinstance(spec, dict):
                names.update(spec.get("aliases") or [])
            real_network = networks.get(network_key, network_key)
            if real_network:
                names.add(f"{name}.{real_network}")
        for alias in names:
            published.setdefault(alias, set()).add(name)
    return published


def _probe_paths_from_source() -> tuple[str, str]:
    """Read the live and ready probe paths out of ``deploy/healthcheck.py``.

    Parsed from the source rather than imported so that the test reads the same
    thing a reader of the file reads, with no import-order dependency on the
    FastAPI import in the other fixture. The regex requires the assignment to
    the exact constant names, so renaming a constant fails here rather than
    silently yielding a wrong pair.
    """
    text = _healthcheck_source()
    found: dict[str, str] = {}
    for name in ("LIVE_PATH", "READY_PATH"):
        match = re.search(rf'^{name}\s*=\s*"([^"]+)"', text, re.M)
        assert match, (
            f"deploy/healthcheck.py no longer assigns {name} to a string literal; "
            "the probe path can no longer be verified against the app"
        )
        found[name] = match.group(1)
    return found["LIVE_PATH"], found["READY_PATH"]


def _probe_port_from_source() -> int:
    match = re.search(r"^BIND_PORT\s*=\s*(\d+)", _healthcheck_source(), re.M)
    assert match, "deploy/healthcheck.py no longer assigns BIND_PORT to a literal"
    return int(match.group(1))


def _compose_doc() -> dict:
    assert COMPOSE_PATH.is_file(), f"missing {COMPOSE_PATH}"
    return _parse_yaml(COMPOSE_PATH.read_text(encoding="utf-8"))


def _prometheus_doc() -> dict:
    assert PROMETHEUS_PATH.is_file(), f"missing {PROMETHEUS_PATH}"
    return _parse_yaml(PROMETHEUS_PATH.read_text(encoding="utf-8"))


# ---------------------------------------------------------------------------
# What the app actually serves
# ---------------------------------------------------------------------------


@pytest.fixture(scope="module")
def served_paths() -> set[str]:
    """Every HTTP path ``create_app()`` registers, read from its OpenAPI schema.

    The OpenAPI document is the app's own statement of what it serves, produced
    by the same route table that serves requests. Enumerating it is what makes
    the probe check load-bearing: a path the app does not register is a path
    that 404s, and a 404 is what a permanently-unhealthy container looks like.

    Built by importing the app rather than by grepping the source, so a router
    that is included under a prefix is counted at the prefixed path it is
    actually reachable at.
    """
    import sys

    if str(INTERFACES_SRC) not in sys.path:
        sys.path.insert(0, str(INTERFACES_SRC))
    from tradex_interfaces.fastapi_app import create_app

    return set(create_app().openapi()["paths"])


@pytest.fixture(scope="module")
def probe_paths(served_paths: set[str]) -> tuple[str, str]:
    live, ready = _probe_paths_from_source()
    # Assert inside the fixture so every test using it sees the same failure
    # rather than each re-deriving a pair that might be stale.
    assert live in served_paths, (
        f"the liveness probe points at {live!r}, which create_app() does not serve. "
        f"It serves: {sorted(served_paths)}"
    )
    assert ready in served_paths, (
        f"the readiness probe points at {ready!r}, which create_app() does not serve. "
        f"It serves: {sorted(served_paths)}"
    )
    return live, ready


# ---------------------------------------------------------------------------
# 1. The probe paths are routes the app actually serves
# ---------------------------------------------------------------------------


def test_probe_paths_are_routes_the_app_serves(probe_paths: tuple[str, str]) -> None:
    """The headline check, asserted directly on the fixture.

    ``/healthz`` is the near-miss this exists for: it is the conventional name,
    the app does not serve it, and a probe pointed there 404s forever. The
    fixture already asserts membership; this pins the fixture's own output so a
    broken assertion cannot pass silently.
    """
    live, ready = probe_paths
    assert live.startswith("/") and ready.startswith("/")
    assert live != METRICS_PATH and ready != METRICS_PATH, (
        "a probe aimed at the metrics endpoint would scrape, not health-check"
    )


def test_probe_paths_are_registered_by_the_health_router(served_paths: set[str]) -> None:
    """Both probes must be real routes, distinct from each other.

    Distinctness is not cosmetic. ``/health`` (the third route in health.py)
    always returns 200 regardless of session state, so pointing EITHER probe at
    it yields a check that can never fail — the specific defect the lane is
    told to avoid. Liveness and readiness must also differ from each other:
    collapsing them onto one endpoint destroys the only thing the pair is for.
    """
    for path in ("/health", "/health/live", "/health/ready"):
        assert path in served_paths, f"expected {path} to be a registered route"

    health_routes = {p for p in served_paths if p.startswith("/health")}
    assert len(health_routes) == 3, (
        f"expected three /health* routes, found {sorted(health_routes)}; a probe "
        "pointed at a fourth would be pointing at a 404"
    )


def test_health_router_serves_both_probes_and_they_are_not_the_same_handler() -> None:
    """``/health/live`` and ``/health/ready`` must be DIFFERENT handlers.

    Read out of the source rather than assumed, because a refactor that merged
    them into one handler would leave both paths serving 200 and this is the
    only place that would notice. The two functions must both exist and must be
    named differently — ``health_live`` and ``health_ready``.
    """
    health_py = (
        REPO_ROOT / "interfaces" / "src" / "tradex_interfaces" / "routes" / "health.py"
    )
    text = health_py.read_text(encoding="utf-8")
    assert 'router.get("/health/live"' in text, "no /health/live route decorator"
    assert 'router.get("/health/ready"' in text, "no /health/ready route decorator"
    assert re.search(r"async def health_live\(", text), "health_live handler is gone"
    assert re.search(r"async def health_ready\(", text), "health_ready handler is gone"


def test_liveness_never_inspects_the_session() -> None:
    """The liveness handler must not depend on the session.

    This is the property that makes liveness liveness. If ``health_live`` ever
    starts consulting ``Depends(get_session)`` or calling ``is_ready``, a
    process that is perfectly healthy but has no market feed starts failing
    liveness, and the orchestrator restarts it — turning a market-data outage
    into a crash loop. The readiness handler is the one that must consult the
    session, and it is asserted to.
    """
    health_py = (
        REPO_ROOT / "interfaces" / "src" / "tradex_interfaces" / "routes" / "health.py"
    )
    text = health_py.read_text(encoding="utf-8")

    live_body = _route_body(text, "health_live")
    ready_body = _route_body(text, "health_ready")

    assert "get_session" not in live_body, (
        "the liveness handler now depends on the session; a not-ready session "
        "would make a healthy process fail liveness and get restarted"
    )
    assert "is_ready" not in live_body, (
        "the liveness handler now calls is_ready; liveness must not depend on "
        "session or feed state"
    )
    # And the mirror image, so the fix is not simply to strip both.
    assert "is_ready" in ready_body, (
        "the readiness handler no longer calls is_ready, so /health/ready can "
        "only ever return 200 and the readiness probe is decorative"
    )
    assert "503" in ready_body, "the readiness handler no longer refuses with 503"


def test_readiness_refuses_a_session_that_is_not_ready() -> None:
    """``/health/ready`` must be able to fail, and must say why.

    A readiness probe pointed at an endpoint that always returns 200 is worse
    than no probe. The 503 and its detail string are what make the readiness
    signal real, so both are asserted here rather than trusted.
    """
    helpers = (
        REPO_ROOT / "interfaces" / "src" / "tradex_interfaces" / "_helpers.py"
    )
    text = helpers.read_text(encoding="utf-8")
    assert "def is_ready(" in text, "is_ready is gone; nothing can gate readiness"
    is_ready_body = _function_body(text, "is_ready")
    # The comparison may name the value directly or through the constant that
    # carries it. The constant is pinned to the same value first, so accepting
    # the second spelling cannot make this pass vacuously.
    from tradex_domain.constants import READY_STATE

    assert READY_STATE == "READY", "READY_STATE no longer holds the READY value"
    assert '"READY"' in is_ready_body or "READY_STATE" in is_ready_body, (
        "is_ready no longer compares the session state against READY"
    )
    assert "supervisor" in is_ready_body, (
        "is_ready no longer consults the feed supervisor, so a dead feed would "
        "still report ready"
    )


def _route_body(text: str, function_name: str) -> str:
    """The source of one decorated route handler, decorators included."""
    return _function_body(text, function_name)


def _function_body(text: str, function_name: str) -> str:
    """Return the source of ``function_name`` up to the next top-level ``def``.

    Indentation-scoped rather than regex-to-blank-line, because these files use
    blank lines inside bodies and a blank-line cut would truncate the check to
    the first two lines — which is how a real violation slips through.
    """
    start = re.search(rf"^(async )?def {re.escape(function_name)}\(", text, re.M)
    assert start, f"{function_name} not found in source"
    rest = text[start.end():]
    next_def = re.search(r"^(async )?def \w+\(|^@", rest, re.M)
    return rest[: next_def.start()] if next_def else rest


# ---------------------------------------------------------------------------
# 2. The Dockerfile starts the app the way the repo actually starts it
# ---------------------------------------------------------------------------


def test_entrypoint_is_the_declared_console_script() -> None:
    """ENTRYPOINT must be ``tradex serve``, not a raw uvicorn factory.

    ``tradex`` is the only console script the repo declares
    (interfaces/pyproject.toml ``[project.scripts]``). A raw
    ``uvicorn tradex_interfaces.fastapi_app:serve_app`` line looks equivalent
    and is not: the factory cannot see its own bind host, which its own
    docstring concedes, so a live deployment would sit outside
    ``_require_live_bind_loopback``. The test therefore asserts the script name
    comes from the package that declares it, not from a hardcoded string.
    """
    text = _dockerfile_text()
    entrypoint = re.search(r"^ENTRYPOINT\s+(\[[^\]]*\])", text, re.M)
    assert entrypoint, "Dockerfile has no exec-form ENTRYPOINT"

    declared = _declared_console_script()
    assert declared, "no console script is declared in interfaces/pyproject.toml"
    parts = [p.strip().strip('"') for p in entrypoint.group(1).strip("[]").split(",")]
    assert parts[0] == declared, (
        f"ENTRYPOINT runs {parts[0]!r}, but the only console script "
        f"interfaces/pyproject.toml declares is {declared!r}"
    )
    assert parts[1] == "serve", (
        f"ENTRYPOINT runs {parts[0]} {parts[1]!r}; the documented serve command is "
        f"{declared!r} serve"
    )


def _declared_console_script() -> str | None:
    """Read the console script name out of interfaces/pyproject.toml.

    A regex rather than a TOML parse because the root pyproject already proves
    this repo's tests avoid a stdlib-adjacent parse when the source is one
    clean block, and because reading the declaration rather than assuming it is
    what makes the ENTRYPOINT check honest.
    """
    pyproject = (REPO_ROOT / "interfaces" / "pyproject.toml").read_text(encoding="utf-8")
    block = re.search(r"\[project\.scripts\](.*?)(?=\n\[|\Z)", pyproject, re.S)
    if not block:
        return None
    for line in block.group(1).splitlines():
        match = re.match(r'^\s*([A-Za-z0-9_.-]+)\s*=\s*"', line)
        if match:
            return match.group(1)
    return None


def test_the_serving_command_binds_the_port_the_probe_uses() -> None:
    """CMD and the probe must agree on the port.

    Split because Docker's exec form does not allow a shell variable: the
    Dockerfile cannot say ``CMD ["--port", "$PORT"]`` and have it expand, so
    the port is written twice and the only thing that catches a mismatch is a
    test. The healthcheck script's BIND_PORT and the CMD's ``--port`` are
    therefore asserted equal here, and separately against the scrape config
    below.
    """
    text = _dockerfile_text()
    cmd_match = re.search(r"^CMD\s+(\[[^\]]*\])", text, re.M)
    assert cmd_match, "Dockerfile has no exec-form CMD"
    cmd = [p.strip().strip('"') for p in cmd_match.group(1).strip("[]").split(",")]

    assert "--port" in cmd, f"CMD does not pass --port: {cmd}"
    cmd_port = cmd[cmd.index("--port") + 1]
    assert cmd_port.isdigit(), f"--port is not a literal port: {cmd_port!r}"

    probe_port = _probe_port_from_source()
    assert int(cmd_port) == probe_port, (
        f"Dockerfile CMD serves on {cmd_port} but deploy/healthcheck.py probes "
        f"{probe_port}; the container would be permanently unhealthy"
    )


def test_dockerfile_exposes_the_served_port() -> None:
    """EXPOSE must name the port the app is told to bind."""
    text = _dockerfile_text()
    expose = re.search(r"^EXPOSE\s+(\d+)", text, re.M)
    assert expose, "Dockerfile does not EXPOSE a port"
    cmd = re.search(r"^CMD\s+(\[[^\]]*\])", text, re.M)
    assert cmd, "Dockerfile has no CMD"
    parts = [p.strip().strip('"') for p in cmd.group(1).strip("[]").split(",")]
    assert expose.group(1) == parts[parts.index("--port") + 1], (
        f"EXPOSE {expose.group(1)} does not match the port the app is started on"
    )


def test_healthcheck_runs_both_probes_from_one_command() -> None:
    """Docker keeps only the LAST HEALTHCHECK, so there must be exactly one.

    This is a real trap rather than a style rule: writing
    ``HEALTHCHECK ... /health/live`` followed by
    ``HEALTHCHECK ... /health/ready`` produces a single healthcheck — the
    readiness one — and the liveness probe silently never runs. The liveness
    probe is then not merely absent but unknowable-absent, since nothing in
    the running container records that a probe was configured and dropped.
    Requiring one HEALTHCHECK that invokes a script running both probes is the
    only shape that actually gives the pair.

    The counting is line-based rather than regex-based over the whole file, and
    that is not a style preference. A pattern like
    ``^HEALTHCHECK\\b.*(?:\\n\\s+.*)*`` looks right and is wrong: ``\\s`` matches
    a newline, so a blank line between the two instructions lets the pattern
    swallow the second HEALTHCHECK — and the ENTRYPOINT after it — as
    continuations of the first. It reports one HEALTHCHECK for a file with two,
    which is precisely the defect this test exists to catch. A blank line is
    legal between Dockerfile instructions, so a mutation that inserts one
    defeats the naive pattern silently.
    """
    instructions = [
        line for line in _dockerfile_instructions().splitlines() if line.strip()
    ]
    healthchecks = [line for line in instructions if line.startswith("HEALTHCHECK")]
    assert len(healthchecks) == 1, (
        f"found {len(healthchecks)} HEALTHCHECK instructions; Docker keeps only "
        "the last, so a liveness/readiness pair must be ONE command running both"
    )
    body = _dockerfile_instructions()
    start = body.index(healthchecks[0])
    body = body[start : start + len(healthchecks[0]) + 400]
    assert "healthcheck.py" in body.split("HEALTHCHECK")[1], (
        "the single HEALTHCHECK does not run deploy/healthcheck.py, so it is "
        "not the pair of probes this lane defines"
    )


def test_healthcheck_timings_are_present_and_ordered() -> None:
    """Every timing flag present, and the ones that interact are ordered right.

    A HEALTHCHECK missing ``--start-period`` is fatal on a cold boot: the
    readiness probe legitimately 503s while the session boots, and without a
    start period those failures count toward ``--retries``, so docker kills a
    container that is still starting. ``--timeout`` must be below
    ``--interval`` (a probe that outlives its period is not a probe), and
    ``--start-interval`` must be below ``--interval`` (it is a faster poll
    during startup, not a slower one).
    """
    text = _dockerfile_instructions()
    match = re.search(r"^HEALTHCHECK\s+([^\n]*(?:\n[^\n#]*)*)", text, re.M)
    assert match, "Dockerfile has no HEALTHCHECK"
    flags = match.group(1)

    def flag(name: str) -> str:
        match = re.search(rf"--{name}=(\S+)", flags)
        assert match, f"HEALTHCHECK is missing --{name}"
        return match.group(1)

    def seconds(name: str) -> float:
        raw = flag(name)
        assert raw.endswith("s"), f"--{name} is not in seconds: {raw!r}"
        return float(raw[:-1])

    interval = seconds("interval")
    timeout = seconds("timeout")
    start_period = seconds("start-period")
    start_interval = seconds("start-interval")

    assert int(flag("retries")) >= 1, "HEALTHCHECK must allow at least one retry"
    assert start_period > 0, (
        "--start-period must be positive: readiness legitimately 503s during "
        "a cold boot and without a start period that restarts the container"
    )
    assert timeout < interval, (
        f"--timeout {timeout}s is not below --interval {interval}s; a probe that "
        "outlives its period is not a probe"
    )
    assert start_interval < interval, (
        f"--start-interval {start_interval}s is not below --interval {interval}s; "
        "start-interval is a FASTER poll during the start period"
    )
    # A start period shorter than the boot is a restart loop; the app builds a
    # session, a reconciliation check and a feed supervisor before it binds.
    assert start_period >= 10, (
        f"--start-period is {start_period}s, too short for this app's cold boot; "
        "a container killed while still starting is the classic restart loop"
    )


def test_the_probe_client_exists_in_the_image() -> None:
    """Whatever runs the probe has to be in the image.

    A HEALTHCHECK that shells out to curl on a ``python:3.12-slim`` base fails
    with "executable file not found" for every probe, which reports as a
    permanently unhealthy container and looks like an application problem. The
    probe here runs on the interpreter already present, so the assertion is
    that the HEALTHCHECK's interpreter is the image's own and that no
    uninstalled external client is named.
    """
    text = _dockerfile_instructions()
    match = re.search(r"^HEALTHCHECK\s+([^\n]*(?:\n[^\n#]*)*)", text, re.M)
    assert match, "Dockerfile has no HEALTHCHECK"
    command = match.group(1)
    assert "CMD [" in command, (
        "the HEALTHCHECK is not in exec form; a shell form would also mean a "
        "second shell process whose exit status Docker would not interpret"
    )
    assert re.search(r'CMD\s*\[\s*"(python|python3)"', command), (
        f"the probe does not run on the image's own interpreter: {command!r}"
    )
    for absent in ("curl", "wget", "nc ", "http"):
        assert absent not in command, (
            f"the HEALTHCHECK invokes {absent!r}, which the image does not install; "
            "a probe whose client is missing reports a healthy container unhealthy"
        )


def test_entrypoint_is_exec_form_so_sigterm_reaches_the_app() -> None:
    """No shell wrapper around ENTRYPOINT.

    ``uvicorn.run`` never returns on SIGTERM — it re-raises the signal inside
    its own ``capture_signals`` block — which is why the app owns teardown in
    its ASGI lifespan (``_lifespan`` in fastapi_app.py) rather than in a
    caller's ``finally``. That teardown only runs if the signal reaches the
    process, so a shell-form ENTRYPOINT (or a ``sh -c`` CMD) would swallow it
    and leak the feed socket and the SQLite handle.
    """
    entrypoint = re.search(r"^ENTRYPOINT\s+(.+)$", _dockerfile_instructions(), re.M)
    assert entrypoint, "Dockerfile has no ENTRYPOINT"
    value = entrypoint.group(1).strip()
    assert value.startswith("["), (
        f"ENTRYPOINT is {value!r}, not exec form; a shell wrapper would swallow "
        "SIGTERM and skip the app's ASGI-lifespan teardown"
    )
    assert not re.search(r"\bsh\b|\bbash\b|\b/bin/", value), (
        "the ENTRYPOINT spawns a shell, which would not forward SIGTERM to `tradex`"
    )


def test_single_process_no_workers_or_reload() -> None:
    """Neither ``--workers`` nor ``--reload`` may be in the run command.

    ``start_fastapi_server`` raises for a live session with either flag, and
    the spec settles the shape as "FastAPI plus uvicorn, one process ... multi
    process is explicitly not scheduled". A CMD that passed ``--workers 4``
    would therefore be an artifact that cannot start in the mode it is meant to
    run in, and in paper mode it would silently give every worker its own
    session — four divergent books.
    """
    for token in ("--workers", "--reload"):
        assert token not in _dockerfile_instructions(), (
            f"the Dockerfile passes {token}; the app refuses it for a live "
            "session and it forks one session per worker in paper mode"
        )


# ---------------------------------------------------------------------------
# 3. The port agrees between the service and the scrape config
# ---------------------------------------------------------------------------


@pytest.fixture(scope="module")
def scrape_target_port() -> int:
    """The port Prometheus is told to scrape the API on."""
    job = _tradex_scrape_job()
    targets = [
        t
        for static in job.get("static_configs") or []
        for t in static.get("targets") or []
    ]
    assert targets, "the tradex scrape job declares no targets"
    return _port_of(str(targets[0]))


def _tradex_scrape_job() -> dict:
    doc = _prometheus_doc()
    jobs = doc.get("scrape_configs") or []
    assert jobs, "prometheus.yml declares no scrape_configs — nothing is collected"
    named = [j for j in jobs if j.get("job_name") == "tradex"]
    assert named, (
        f"no scrape job named 'tradex'; found {[j.get('job_name') for j in jobs]}"
    )
    return named[0]


def test_scrape_config_targets_the_api_service() -> None:
    """The scrape target host must be the compose service name.

    A target of ``localhost`` inside the Prometheus container points at
    Prometheus itself, not the API: the scrape succeeds, returns Prometheus's
    own metrics, and every TradeX alert stays green forever because the series
    it evaluates were never collected. That is the same failure shape as an
    alert on a metric nothing emits, one layer up.
    """
    job = _tradex_scrape_job()
    targets = [
        t
        for static in job.get("static_configs") or []
        for t in static.get("targets") or []
    ]
    compose = _compose_doc()
    services = compose.get("services") or {}
    assert "api" in services, "compose.yml has no 'api' service"
    for target in targets:
        host = _host_of(str(target))
        assert host in services, (
            f"scrape target host {host!r} is not a compose service ({sorted(services)}); "
            "inside the Prometheus container localhost is Prometheus itself"
        )


def test_scrape_path_is_a_route_the_app_serves() -> None:
    """``metrics_path`` must be a registered route.

    Cross-checked against the live OpenAPI schema for the same reason the probe
    paths are: a scrape of a path the app does not serve returns Prometheus's
    own 404 page as exposition text, which parses to zero series and no error.
    """
    job = _tradex_scrape_job()
    assert job.get("metrics_path") == METRICS_PATH, (
        f"the tradex job scrapes {job.get('metrics_path')!r}, not {METRICS_PATH!r}"
    )
    assert METRICS_PATH in _served_paths_cached(), (
        f"the app does not serve {METRICS_PATH}; the scrape collects nothing"
    )


def test_scrape_path_is_registered_by_the_app() -> None:
    """Asserted directly rather than only through the fixture, for the same
    reason as :func:`test_probe_paths_are_routes_the_app_serves`."""
    assert METRICS_PATH in _served_paths_cached()


_SERVED_PATHS: set[str] | None = None


def _served_paths_cached() -> set[str]:
    """Module-cached ``create_app().openapi()["paths"]``.

    Building the app is not free and every test wanting the route set should
    see the same one, so this caches rather than each fixture rebuilding it.
    """
    global _SERVED_PATHS
    if _SERVED_PATHS is None:
        import sys

        if str(INTERFACES_SRC) not in sys.path:
            sys.path.insert(0, str(INTERFACES_SRC))
        from tradex_interfaces.fastapi_app import create_app

        _SERVED_PATHS = set(create_app().openapi()["paths"])
    return _SERVED_PATHS


def test_service_and_scrape_config_agree_on_the_port(scrape_target_port: int) -> None:
    """The scrape port must equal the port the service is actually serving on.

    Cross-checked against the compose command rather than against a constant,
    so the chain is: CLI default -> Dockerfile CMD -> healthcheck BIND_PORT ->
    compose command -> Prometheus target. One of these changing without the
    others produces a container that is healthy and a Prometheus that scrapes
    nothing, and no other test in the repo would notice.
    """
    service_port = _compose_api_port()
    assert service_port == scrape_target_port, (
        f"compose serves the API on {service_port} but prometheus.yml scrapes "
        f"{scrape_target_port}; the scrape collects nothing and every alert in "
        "lane 5b's rule file stays green"
    )


def _compose_api_port() -> int:
    """The container port the compose ``api`` service is started on."""
    compose = _compose_doc()
    api = (compose.get("services") or {}).get("api") or {}
    command = api.get("command")
    assert isinstance(command, list), (
        f"the api service's command is not a list, so the port cannot be read: {command!r}"
    )
    assert "--port" in command, f"the api service does not pass --port: {command}"
    return int(command[command.index("--port") + 1])


def test_published_host_port_maps_to_the_container_port() -> None:
    """``ports:`` must forward the port the service binds.

    A mapping of ``"9090:8080"`` is correct and ``"8080:9090"`` is a container
    that is healthy on 8080 and unreachable from the host on the port the
    README and every curl in the docs use.
    """
    compose = _compose_doc()
    api = (compose.get("services") or {}).get("api") or {}
    mappings = api.get("ports") or []
    assert mappings, "the api service publishes no port"
    container_port = _compose_api_port()
    matched = [m for m in mappings if m.rsplit(":", 1)[1] == str(container_port)]
    assert matched, (
        f"no published port forwards to the container's {container_port}: {mappings}"
    )


def test_scrape_interval_leaves_enough_samples_for_the_shortest_rule_window(
    scrape_target_port: int,
) -> None:
    """The scrape interval must be short enough for 5b's fastest rule.

    ``test_alert_rules.py`` proved every rule names a metric the code emits. It
    could not check that anything COLLECTS those metrics often enough to
    evaluate. Prometheus needs at least two samples in a range to extrapolate a
    rate, and a 10m window on a 5m scrape interval leaves the two samples
    exactly at the edge, so an alert that is genuinely firing computes as no
    data at all — a green alert on a real fault.
    """
    doc = _prometheus_doc()
    global_cfg = doc.get("global") or {}
    interval_raw = global_cfg.get("scrape_interval")
    assert interval_raw, "prometheus.yml sets no global.scrape_interval"
    assert str(interval_raw).endswith("s"), f"unexpected interval unit: {interval_raw!r}"
    interval = float(str(interval_raw)[:-1])

    shortest_window_s = _shortest_rule_window_seconds()
    samples = shortest_window_s / interval
    assert samples >= 2, (
        f"a {shortest_window_s}s rule window holds only {samples:.1f} samples at a "
        f"{interval}s scrape interval; Prometheus needs at least 2 to evaluate a rate"
    )
    timeout_raw = global_cfg.get("scrape_timeout")
    if timeout_raw:
        timeout = float(str(timeout_raw)[:-1])
        assert timeout <= interval, (
            f"scrape_timeout {timeout_raw} exceeds scrape_interval {interval_raw}; "
            "Prometheus rejects the whole config"
        )
    # The port is an argument so the signature makes the relationship explicit.
    assert scrape_target_port == _compose_api_port()


def _shortest_rule_window_seconds() -> float:
    """The smallest range window any rule in 5b's file uses, in seconds."""
    text = RULES_PATH.read_text(encoding="utf-8")
    windows = re.findall(r"\[(\d+)([smh])\]", text)
    assert windows, "no range window found in prometheus-rules.yml"
    factors = {"s": 1.0, "m": 60.0, "h": 3600.0}
    return min(float(v) * factors[u] for v, u in windows)


# ---------------------------------------------------------------------------
# 4. The alerting path is closed
# ---------------------------------------------------------------------------


def test_prometheus_loads_lane_5bs_rule_file() -> None:
    """``rule_files`` must point at the file lane 5b actually wrote.

    A rule file with no loader is 20 alerts that never evaluate. The path here
    is the CONTAINER path, so the real assertion is that compose mounts 5b's
    file at exactly that path — which is the next test.
    """
    doc = _prometheus_doc()
    rule_files = doc.get("rule_files") or []
    assert rule_files, "prometheus.yml lists no rule_files, so no alert ever fires"
    joined = " ".join(str(f) for f in rule_files)
    assert RULES_PATH.name in joined, (
        f"rule_files {rule_files} do not reference {RULES_PATH.name}"
    )


def test_compose_mounts_the_rule_file_where_the_config_claims() -> None:
    """The mount target must equal the path in ``rule_files``.

    This is the check that makes the previous one mean anything. ``rule_files``
    names a container path; if compose mounted the rules somewhere else,
    Prometheus would fail to start on a missing-file error, or — worse, with
    ``--config.file`` pointing at a valid file and the rule path simply absent
    — start cleanly and evaluate nothing.
    """
    doc = _prometheus_doc()
    rule_paths = [str(f) for f in doc.get("rule_files") or []]
    compose = _compose_doc()
    prometheus = (compose.get("services") or {}).get("prometheus") or {}
    mounted = _container_mounts(prometheus)
    assert mounted, "the prometheus service mounts nothing"

    for path in rule_paths:
        assert path in mounted, (
            f"rule_files names {path!r} but the prometheus service mounts "
            f"{sorted(mounted)}; the rules would never load"
        )
        assert mounted[path].endswith(RULES_PATH.name), (
            f"{path} is backed by {mounted[path]!r}, not lane 5b's rules file"
        )


def test_compose_mounts_the_prometheus_config_it_is_told_to_read() -> None:
    """``--config.file`` and the mount must name the same path.

    The obvious mistake is mounting the config at /etc/prometheus/prometheus.yml
    and starting the container with ``--config.file=/etc/prometheus/prom.yml``.
    Prometheus then exits immediately and the failure is a log line nobody
    reads.
    """
    compose = _compose_doc()
    prometheus = (compose.get("services") or {}).get("prometheus") or {}
    command = " ".join(str(c) for c in prometheus.get("command") or [])
    match = re.search(r"--config\.file=(\S+)", command)
    assert match, "the prometheus service does not pass --config.file"
    config_path = match.group(1)
    assert config_path.endswith("/" + PROMETHEUS_PATH.name), (
        f"--config.file={config_path} does not name {PROMETHEUS_PATH.name}"
    )
    targets = _container_mounts(prometheus)
    assert config_path in targets, (
        f"--config.file={config_path} is not mounted; the container has no config"
    )


def test_alertmanager_is_given_lane_5bs_config() -> None:
    """``--config.file`` must be backed by ``deploy/alertmanager.yml``.

    A Prometheus pointed at an Alertmanager that was never configured, or one
    running a different config, breaks the last link: alerts evaluate, get
    delivered to a receiver tree that does not exist, and vanish.
    """
    compose = _compose_doc()
    alertmanager = (compose.get("services") or {}).get("alertmanager") or {}
    command = " ".join(str(c) for c in alertmanager.get("command") or [])
    match = re.search(r"--config\.file=(\S+)", command)
    assert match, "the alertmanager service does not pass --config.file"

    sources = _container_mounts(alertmanager)
    config_path = match.group(1)
    assert config_path in sources, (
        f"--config.file={config_path} is not mounted into the alertmanager"
    )
    assert sources[config_path].endswith(ALERTMANAGER_PATH.name), (
        f"{config_path} is backed by {sources[config_path]!r}, not lane 5b's "
        "alertmanager.yml"
    )


def test_prometheus_is_told_where_the_alertmanager_is() -> None:
    """``alerting.alertmanagers`` must name a compose service.

    Same failure as the scrape target pointing at localhost, inverted: a
    Prometheus that cannot reach an Alertmanager re-sends every alert to
    /-/reload and the routing is never applied.
    """
    doc = _prometheus_doc()
    managers = (doc.get("alerting") or {}).get("alertmanagers") or []
    assert managers, "prometheus.yml has no alerting.alertmanagers, so alerts are dropped"
    compose = _compose_doc()
    services = compose.get("services") or {}
    for entry in managers:
        targets = [
            t for s in entry.get("static_configs") or [] for t in s.get("targets") or []
        ]
        assert targets, "an alertmanager entry declares no targets"
        for target in targets:
            host = _host_of(str(target))
            assert host in services, (
                f"alertmanager target host {host!r} is not a compose service"
            )
            port = _port_of(str(target))
            published = " ".join(str(p) for p in services[host].get("ports") or [])
            assert f":{port}" in published, (
                f"the {host} service publishes {published!r}, not port {port} the "
                "scrape config points at"
            )


def test_alertmanager_delivers_to_a_name_compose_publishes() -> None:
    """The last link in the alerting chain, and the one this stack relies on.

    Lane 5b's ``alertmanager.yml`` points both receivers at
    ``http://alertmanager-webhook.tradex.svc.cluster.local:8080/...``. That
    file is another lane's and is not edited here. The Alertmanager is
    therefore started with ``--webhook.url=http://alertmanager-webhook:8080``:
    the CLI flag overrides the config, and the flag wins, so lane 5b's routing
    is consumed exactly as written while the thing that is actually listening
    is a compose service. A receiver URL pointing at nothing is an alert that
    fires, routes and is delivered nowhere — a stack that looks complete and
    pages no one — so this asserts the host and the port it names are both real.

    The flag is the load-bearing part and is checked first. The FQDN from 5b's
    file is checked second and more weakly, because making *that* string
    resolve depends on a Docker aliasing behaviour that was checked against
    the reference rather than against a live daemon; the network carrying 5b's
    name is what the stack guarantees.
    """
    compose = _compose_doc()
    services = compose.get("services") or {}
    alertmanager = services.get("alertmanager") or {}
    command = " ".join(str(c) for c in alertmanager.get("command") or [])

    override = re.search(r"--webhook\.url=https?://([^/\s]+)", command)
    assert override, (
        "the alertmanager service does not pass --webhook.url, so lane 5b's "
        "receiver URL is the one used verbatim and the route has to resolve on "
        "its own"
    )
    target = override.group(1)
    host, port = _host_of(target), _port_of(target)
    assert host in services, (
        f"--webhook.url points at {host!r}, which is not a compose service "
        f"({sorted(services)}); every alert would be delivered to nothing"
    )
    assert port == _receiver_port(), (
        f"--webhook.url points at port {port} but deploy/webhook-receiver.py "
        f"binds {_receiver_port()}"
    )
    assert "webhook-receiver.py" in " ".join(
        str(c) for c in (services[host].get("command") or [])
    ), (
        f"the service on {host!r} is not the webhook receiver, so the "
        "Alertmanager's POSTs would land on something else"
    )


def test_lane_5bs_receiver_fqdn_names_this_stack_not_a_bare_cluster() -> None:
    """The name in 5b's config must belong to this stack, by one route or another.

    Accepted: a compose service by that name, an explicit alias, the service
    joined to a network whose name is the dotted tail, or a network carrying
    the whole FQDN. Refused: a name nothing in ``deploy/`` accounts for, which
    is the case where a stack reads as complete and pages no one.
    """
    text = ALERTMANAGER_PATH.read_text(encoding="utf-8")
    urls = sorted(set(re.findall(r"https?://([^/\s\"']+)", text)))
    assert urls, "no webhook URL found in lane 5b's alertmanager.yml"

    published = _compose_aliases()
    networks = {
        (value or {}).get("name") or key
        for key, value in (_compose_doc().get("networks") or {}).items()
    }
    receiver_port = _receiver_port()

    unresolved: list[str] = []
    for hostport in urls:
        host, port = _host_of(hostport), _port_of(hostport)
        if host in published:
            if port != receiver_port:
                unresolved.append(
                    f"{hostport}: the service on that name binds "
                    f"{receiver_port}, not {port}"
                )
            continue
        if not any(host.endswith(f".{n}") or host == n for n in networks):
            unresolved.append(
                f"{host}: nothing in deploy/ publishes it. compose publishes "
                f"{sorted(published)} and networks {sorted(networks)}"
            )
    assert not unresolved, (
        "alertmanager receivers point at nothing:\n" + "\n".join(unresolved)
    )


def _receiver_port() -> int:
    """The port ``deploy/webhook-receiver.py`` actually binds.

    Read by loading the module rather than by grepping the source, because the
    property is what the code does at import time, not what a constant says.
    """
    import importlib.util

    spec = importlib.util.spec_from_file_location(
        "tradex_deploy_receiver", RECEIVER_PATH
    )
    assert spec and spec.loader, f"could not load {RECEIVER_PATH}"
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return int(module.BIND_PORT)


def test_the_receiver_serves_the_paths_lane_5b_posts_to() -> None:
    """Every path 5b posts to must be one the receiver answers.

    The other half of the previous test: a receiver that binds the right port
    and answers 404 on ``/pager-critical`` is a route that looks wired and is
    not. Asserted by loading the module and comparing the paths it declares
    against the paths in the URLs, in both directions — a receiver that 404s a
    posted path and a rule file pointing at a path the receiver never had are
    the same failure.
    """
    text = ALERTMANAGER_PATH.read_text(encoding="utf-8")
    posted = sorted(
        {path for path in re.findall(r"https?://[^/\s\"']+(/\S*?)(?:\s|$|\")", text) if path}
    )
    assert posted, "no webhook path found in lane 5b's alertmanager.yml"

    import importlib.util

    spec = importlib.util.spec_from_file_location(
        "tradex_deploy_receiver_paths", RECEIVER_PATH
    )
    assert spec and spec.loader
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)

    declared = set(module.KNOWN_PATHS)
    assert declared, "the receiver declares no known paths"
    for path in posted:
        assert path in declared, (
            f"lane 5b posts to {path!r} but deploy/webhook-receiver.py only "
            f"handles {sorted(declared)}; the alert would land on a 404"
        )
    for path in sorted(declared - set(posted)):
        assert path not in text, (
            f"the receiver advertises {path!r} but no receiver URL in lane 5b's "
            "alertmanager.yml posts there"
        )


def test_the_receiver_sink_is_stdlib_only() -> None:
    """The sink must not import anything that has to be installed.

    ``python:3.12-slim`` with no install step is the whole reason this file
    exists as a standalone module. A third-party import would make the alerting
    path depend on a wheel resolving at container start, which is a failure mode
    strictly worse than having no sink: the stack looks configured and the
    receiver is down.
    """
    text = RECEIVER_PATH.read_text(encoding="utf-8")
    imports = set(re.findall(r"^(?:from|import)\s+([A-Za-z_][\w.]*)", text, re.M))
    third_party = imports - {
        "__future__",
        "argparse",
        "json",
        "os",
        "sys",
        "http.server",
        "typing",
    }
    assert not third_party, (
        f"deploy/webhook-receiver.py imports {sorted(third_party)}; the image "
        "installs nothing, so the receiver could not start"
    )


# ---------------------------------------------------------------------------
# 5. The probe is real: it is executed here, against a live app
# ---------------------------------------------------------------------------


def _load_healthcheck_module():
    """Import ``deploy/healthcheck.py`` by path.

    It is a deploy artifact rather than a package module, so it is loaded from
    its file. That is also what makes this an execution test: the file the
    Dockerfile names is the file being run, not a re-implementation.
    """
    import importlib.util

    spec = importlib.util.spec_from_file_location("tradex_deploy_healthcheck", HEALTHCHECK_PATH)
    assert spec and spec.loader, f"could not load {HEALTHCHECK_PATH}"
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


@pytest.fixture(scope="module")
def healthcheck():
    return _load_healthcheck_module()


def _serve_real_app(app) -> tuple[BaseHTTPRequestHandler, str, threading.Thread, Any]:
    """Serve *app* on a free loopback port in a real HTTP server.

    A real socket rather than a mocked urlopen, because the property under test
    is that the probe script actually reaches the app over HTTP and classifies
    what comes back. Mocking the transport would prove the script calls the
    function it was written to call, which is not the property.
    """
    from fastapi.testclient import TestClient

    client = TestClient(app)

    class _Handler(BaseHTTPRequestHandler):
        protocol_version = "HTTP/1.1"

        def do_GET(self) -> None:  # noqa: N802
            response = client.get(self.path)
            payload = response.content
            self.send_response(response.status_code)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(payload)))
            self.end_headers()
            self.wfile.write(payload)

        def log_message(self, *args) -> None:
            """Silence the access log."""

    sock = socket.socket()
    sock.bind(("127.0.0.1", 0))
    port = sock.getsockname()[1]
    sock.close()
    server = HTTPServer(("127.0.0.1", port), _Handler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    return _Handler, port, thread, server


def _free_port() -> int:
    sock = socket.socket()
    sock.bind(("127.0.0.1", 0))
    port = sock.getsockname()[1]
    sock.close()
    return port


def test_the_probe_passes_against_a_ready_app(healthcheck) -> None:
    """End to end: a READY session makes ``run_all`` exit 0.

    A ready session is a stub whose state is ``READY`` and which has no feed
    supervisor, which is the paper/backtest shape ``_helpers.is_ready`` treats
    as ready (``supervisor is None or bool(supervisor.ready)``).
    """
    import sys

    if str(INTERFACES_SRC) not in sys.path:
        sys.path.insert(0, str(INTERFACES_SRC))
    from tradex_interfaces.fastapi_app import create_app

    class _ReadySession:
        state = "READY"

    _, port, _, server = _serve_real_app(create_app(_ReadySession()))
    try:
        assert healthcheck.run_all(port) == healthcheck.EXIT_OK, (
            "the probe did not report a READY session as healthy"
        )
        assert healthcheck.probe("/health/live", port).ok
        assert healthcheck.probe("/health/ready", port).ok
    finally:
        server.shutdown()
        server.server_close()


def test_the_probe_fails_with_a_distinct_exit_code_when_not_ready(healthcheck) -> None:
    """A non-READY session must fail readiness with its own exit code.

    The distinction is the point. A probe that cannot tell "the process is
    gone" from "the process is up but the session never reached READY" gives an
    operator one bit where they need two, and the two call for opposite
    responses.
    """
    import sys

    if str(INTERFACES_SRC) not in sys.path:
        sys.path.insert(0, str(INTERFACES_SRC))
    from tradex_interfaces.fastapi_app import create_app

    class _NewSession:
        state = "NEW"

    _, port, _, server = _serve_real_app(create_app(_NewSession()))
    try:
        live = healthcheck.probe("/health/live", port)
        ready = healthcheck.probe("/health/ready", port)
        assert live.ok, "liveness must still pass for a not-ready session"
        assert not ready.ok, "readiness must fail for a NEW session"
        assert ready.status == 503, (
            f"readiness returned {ready.status}, expected the route's own 503"
        )
        code = healthcheck.run_all(port)
        assert code == healthcheck.EXIT_NOT_READY, (
            f"run_all returned {code}; expected EXIT_NOT_READY "
            f"({healthcheck.EXIT_NOT_READY}) so the failure is distinguishable "
            "from a dead process"
        )
        assert code != healthcheck.EXIT_NOT_SERVING
    finally:
        server.shutdown()
        server.server_close()


def test_the_probe_reports_a_dead_process_differently(healthcheck) -> None:
    """Nothing listening is EXIT_NOT_SERVING, and it is not exit 0.

    A probe that returns 0 when it cannot reach anything is a probe that
    declares a dead container healthy, which is worse than not having one.
    """
    port = _free_port()
    result = healthcheck.probe("/health/live", port)
    assert not result.ok
    assert result.status == 0, f"expected no HTTP status, got {result.status}"
    assert healthcheck.run_all(port) == healthcheck.EXIT_NOT_SERVING


def test_the_probe_would_fail_on_a_path_the_app_does_not_serve(healthcheck) -> None:
    """The negative control: a conventional-but-wrong probe path fails.

    ``/healthz`` is the name almost every framework uses and this app does not
    serve. If this test were to pass with a 200 here it would mean the probe
    treats any 2xx as healthy regardless of what it asked for — which is
    exactly the property that makes a renamed route invisible.
    """
    import sys

    if str(INTERFACES_SRC) not in sys.path:
        sys.path.insert(0, str(INTERFACES_SRC))
    from tradex_interfaces.fastapi_app import create_app

    _, port, _, server = _serve_real_app(create_app())
    try:
        result = healthcheck.probe("/healthz", port)
        assert not result.ok, (
            "/healthz is not a route this app serves, so probing it must fail"
        )
        assert result.status == 404
        assert "/healthz" not in _served_paths_cached()
    finally:
        server.shutdown()
        server.server_close()


def test_probe_order_is_liveness_first(healthcheck) -> None:
    """``run_all`` must ask liveness first and stop there if it fails.

    If readiness were asked first against a process that is not serving, the
    connection error would be attributed to readiness and an operator would go
    looking at the feed supervisor for a process that has no HTTP server at all.
    """
    import sys

    if str(INTERFACES_SRC) not in sys.path:
        sys.path.insert(0, str(INTERFACES_SRC))
    from tradex_interfaces.fastapi_app import create_app

    class _NewSession:
        state = "NEW"

    _, port, _, server = _serve_real_app(create_app(_NewSession()))
    try:
        # Both probes are reachable, so ordering is asserted structurally: the
        # live result must be the one that decides when live fails, which is
        # the only case where the order is observable without instrumenting.
        assert healthcheck.probe(healthcheck.LIVE_PATH, port).ok
        code = healthcheck.run_all(port)
        assert code == healthcheck.EXIT_NOT_READY
        # A dead port short-circuits on liveness, never reaching readiness.
        dead = _free_port()
        assert healthcheck.run_all(dead) == healthcheck.EXIT_NOT_SERVING
    finally:
        server.shutdown()
        server.server_close()


def test_the_probe_script_actually_runs_as_a_process(healthcheck) -> None:
    """``python deploy/healthcheck.py`` must work as a CLI, not just as a module.

    The Dockerfile's HEALTHCHECK invokes it as a subprocess with no arguments,
    so a ``main`` that only worked with arguments — or that read ``sys.argv``
    from the surrounding pytest run — would pass every test above and fail in
    the container. This runs the real entry point with a real argv and a
    genuinely dead port, which is the one case that needs no server.
    """
    import subprocess
    import sys

    port = _free_port()
    completed = subprocess.run(
        [sys.executable, str(HEALTHCHECK_PATH), "--port", str(port)],
        capture_output=True,
        text=True,
        timeout=30,
    )
    assert completed.returncode == healthcheck.EXIT_NOT_SERVING, (
        f"the probe CLI exited {completed.returncode} against a dead port; "
        f"expected {healthcheck.EXIT_NOT_SERVING}. stdout={completed.stdout!r}"
    )
    assert "/health/live" in completed.stdout, (
        "the probe CLI did not report the liveness path it tried"
    )


def test_a_slow_server_does_not_look_healthy(healthcheck) -> None:
    """A response slower than the probe timeout is a failure, not a pass.

    Without this, a hung app is indistinguishable from a slow one and a hung
    app is precisely the case a liveness probe exists to catch.
    """
    class _SlowHandler(BaseHTTPRequestHandler):
        protocol_version = "HTTP/1.1"

        def do_GET(self) -> None:  # noqa: N802
            time.sleep(3.0)
            self.send_response(200)
            self.send_header("Content-Length", "2")
            self.end_headers()
            self.wfile.write(b"ok")

        def log_message(self, *args) -> None:
            """Silence the access log."""

    server = HTTPServer(("127.0.0.1", 0), _SlowHandler)
    port = server.server_address[1]
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        result = healthcheck.probe(
            "/health/live", port, timeout=healthcheck.PROBE_TIMEOUT_SECONDS
        )
        assert not result.ok, "a response slower than the probe timeout was accepted"
    finally:
        server.shutdown()
        server.server_close()


_ROOT = Path(__file__).resolve().parents[2]


def test_every_copied_workspace_manifest_exists() -> None:
    instructions = _dockerfile_instructions()
    targets = re.findall(r"^COPY\s+(\S+)/pyproject\.toml\s", instructions, re.MULTILINE)
    assert targets, "the Dockerfile no longer copies any workspace manifest"
    missing = [
        target
        for target in targets
        if not (_ROOT / target / "pyproject.toml").is_file()
    ]
    assert not missing, f"COPY names a manifest that does not exist: {missing}"


def test_the_container_puts_the_venv_on_path() -> None:
    assert 'PATH="/app/.venv/bin:${PATH}"' in _dockerfile_instructions()


def test_dockerignore_excludes_secrets_and_runtime_state() -> None:
    dockerignore = _ROOT / ".dockerignore"
    assert dockerignore.is_file(), "a Docker build must not send the whole tree"
    patterns = set(dockerignore.read_text(encoding="utf-8").splitlines())
    required = {
        ".git",
        ".venv",
        "**/.venv",
        ".env",
        ".env.*",
        "**/__pycache__",
        "**/*.pyc",
        "runtime/",
        "**/*.sqlite",
        "**/*.db",
    }
    assert required <= patterns, f"missing ignore patterns: {sorted(required - patterns)}"
