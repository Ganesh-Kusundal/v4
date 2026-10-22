# TradeX v4 — API container image (AC-17, lane 5f).
#
# The ENTRYPOINT is `tradex serve`, not `uvicorn tradex_interfaces.fastapi_app:serve_app`.
# That is not a preference, it is what the repo enforces:
#
#   * `tradex` is the ONLY declared console script (interfaces/pyproject.toml
#     [project.scripts] -> tradex_interfaces.cli:main), and README.md documents
#     it as the way to expose the session over HTTP.
#   * `cmd_serve` runs two gates the raw ASGI factory cannot run. It passes the
#     real bind host to `_require_live_bind_loopback`, and it reuses an already
#     booted paper session rather than booting a second one. `serve_app`'s own
#     docstring concedes the gap: "A direct uvicorn
#     tradex_interfaces.fastapi_app:serve_app command cannot expose its actual
#     bind host to this factory, so direct factory invocation is outside
#     enforcement of the loopback policy."
#   * `--workers/--reload` is REFUSED for a live session (start_fastapi_server
#     raises), so this is a single-process container by construction. Spec 0002
#     settles it: "FastAPI plus uvicorn, one process ... multi process is
#     explicitly not scheduled."
#
# Port: 8080, the `tradex serve` default (cli.py:79), the port the README
# documents, the port EXPOSEd here, the port compose publishes and the port
# deploy/prometheus.yml scrapes. All five are asserted to agree by
# observability/tests/test_deploy_artifacts.py.
#
# Validated by observability/tests/test_deploy_artifacts.py.

FROM python:3.12-slim

# No probe client is installed. The probe runs on the interpreter that is
# already here, so the image carries no curl and no extra system package that
# could go missing and turn a healthy container unhealthy.

ENV PATH="/app/.venv/bin:${PATH}" \
    PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    PIP_NO_CACHE_DIR=1 \
    UV_COMPILE_BYTECODE=1 \
    UV_LINK_MODE=copy

WORKDIR /app

# Dependency metadata first so the layer caches across source edits. The
# workspace members are the whole install: `tradex serve` boots a session that
# imports domain, config, brokers, runtime, execution, market_data and more, so
# installing only `tradex-interfaces` would produce an image that fails at boot.
COPY pyproject.toml uv.lock ./
COPY domain/pyproject.toml ./domain/
COPY brokers/pyproject.toml ./brokers/
COPY config/pyproject.toml ./config/
COPY observability/pyproject.toml ./observability/
COPY analytics/pyproject.toml ./analytics/
COPY reactive/pyproject.toml ./reactive/
COPY execution/pyproject.toml ./execution/
COPY strategy/pyproject.toml ./strategy/
COPY replay/pyproject.toml ./replay/
COPY application/pyproject.toml ./application/
COPY market_data/pyproject.toml ./market_data/
COPY interfaces/pyproject.toml ./interfaces/
COPY runtime/pyproject.toml ./runtime/
COPY services/duckdb-analytics/pyproject.toml ./services/duckdb-analytics/

# The manifests alone are not a resolvable lock, so the sources come in too.
# `uv sync` is what CI already uses (.github/workflows/quality-gate.yml), so
# the image resolves dependencies exactly the way the gates do.
COPY . .
RUN pip install --no-cache-dir uv \
 && uv sync --frozen --no-dev

# Two build-time assertions, because both of these are failures that would
# otherwise only show up as a container that will not start:
#   * ENTRYPOINT names `tradex`, so bin/tradex has to exist.
#   * HEALTHCHECK names deploy/healthcheck.py, so that file has to be here.
RUN test -x /app/.venv/bin/tradex \
 && test -f /app/deploy/healthcheck.py

# Non-root. The SQLite event log, workspace.sqlite and the parquet datalake are
# all bind-mounted state, so the uid is an operator decision; 10001 is arbitrary
# but stable. Compose maps it to the host uid, so a volume the container writes
# stays readable on the host.
RUN useradd --uid 10001 --create-home --shell /usr/sbin/nologin tradex \
 && chown -R tradex:tradex /app

USER tradex

# 8080 is the `tradex serve` default and the port deploy/prometheus.yml scrapes.
# Bound on all interfaces in PAPER mode; `_require_live_bind_loopback` is what
# refuses a non-loopback bind for a live broker, so this is the app's own
# policy, not the image's.
EXPOSE 8080

ENV TRADEX_RUNTIME_DIR=/app/runtime \
    TRADEX_DATALAKE_ROOT=/app/data

# ---------------------------------------------------------------------------
# The probe block (AC-17).
# ---------------------------------------------------------------------------
# Docker has ONE healthcheck per container, not a liveness/readiness pair, so
# both probes live in one command: deploy/healthcheck.py runs /health/live then
# /health/ready and exits 0 / 1 / 2 for ok / not serving / not ready. Two
# separate HEALTHCHECK lines would NOT be a probe pair — Docker keeps only the
# last one, so the liveness probe would silently vanish. A platform with real
# separate probes calls the same script with --probe live / --probe ready.
#
# The two endpoints assert genuinely different things, which is why both are
# worth running:
#
#   /health/live   routes/health.py returns a literal
#                  HealthResponse(status="ok", check="live"). It never looks at
#                  the session, the feed or the broker. "Is this process still
#                  serving HTTP?" A liveness probe that consulted session state
#                  would restart a process that is healthy and merely has no
#                  feed, turning a market-data outage into a restart loop.
#
#   /health/ready  routes/health.py raises 503 unless
#                  _helpers.is_ready(session), which requires
#                  str(session.state) == "READY" AND (no feed supervisor OR
#                  supervisor.ready). "Can this process trade right now?" It is
#                  200 for a session-less app, because is_ready(None) is True
#                  by design; the guarantee that a SERVING process has a READY
#                  session is the pre-bind probe in start_fastapi_server, which
#                  refuses to bind at all otherwise.
#
# Both endpoints are exempt from require_auth (only routes/chart.py opts in),
# so the probe needs no credential. See the test
# "test_auth_get_endpoints_require_key_except_probes".
#
# Timings, and the reason for each:
#
#   --start-period 30s    Deliberately generous. A cold boot constructs the
#                         session, the reconciliation check and the feed
#                         supervisor, and /health/ready legitimately 503s for
#                         part of it. Killing during that window is the classic
#                         restart loop, and it is the single most likely way a
#                         probe makes a working deployment look broken. During
#                         start-period a failure is recorded but never fatal.
#   --start-interval 2s   Honoured only inside start-period, so the moment the
#                         session reaches READY is noticed in seconds rather
#                         than up to 30s later.
#   --interval 10s        Fits the alerting window the 5b rules assume: the
#                         shortest range in prometheus-rules.yml is a 10m
#                         increase, and prometheus.yml scrapes every 15s, so a
#                         10m window holds 40 samples — far above the two
#                         Prometheus needs to extrapolate a rate.
#   --timeout 3s          A probe that outlives its period is not a probe.
#                         The script's own per-request timeout is 2s, so a slow
#                         answer is reported as a slow answer.
#   --retries 3           Three consecutive failures before declaring the
#                         container unhealthy: 30s of evidence at 10s apart,
#                         not one unlucky packet.
HEALTHCHECK --interval=10s --timeout=3s --start-period=30s --start-interval=2s --retries=3 \
  CMD ["python", "/app/deploy/healthcheck.py"]

# Exec form, no shell: the container's PID 1 is `tradex` itself, so SIGTERM from
# `docker stop` reaches the process and the app's own ASGI lifespan
# (_lifespan in fastapi_app.py) runs session.stop() on the way out. A shell
# wrapper would swallow the signal and leak the feed socket and the SQLite
# handle, which is precisely the defect docs/specs/remediation-and-hardening.md
# was written to close.
ENTRYPOINT ["tradex", "serve"]
CMD ["--host", "0.0.0.0", "--port", "8080", "--broker", "PAPER"]
