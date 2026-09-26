# Production Authentication Design

**Date:** 2026-09-23
**Status:** DESIGN ONLY — UNIMPLEMENTED
**Scope:** Task 8 audit and documentation slice

## 1. Decision

Production browser authentication is deferred. The current shared API-key/page-injection model is development-only and must not be treated as a production authentication boundary.

Until the target design below is implemented and its acceptance tests pass, live serving remains restricted to the active loopback-only bind gate. A live process may be started only with a non-empty API key on a normalized loopback host. Any non-loopback live bind fails before Uvicorn starts. This document does not implement authentication, change production code, remove the API key, or enable public serving.

## 2. Current boundary and evidence

The following behavior was observed in the current worktree and is the baseline for this design:

| Boundary | Current behavior | Consequence |
| --- | --- | --- |
| HTTP writes | `interface/auth.py:18-31` checks an optional `X-API-Key`; when `app.state.api_key` is `None`, the dependency allows the request. | An unset key is an intentional paper/development bypass, not a fail-closed production default. |
| Browser key delivery | `interface/fastapi_app.py:200-209` injects the configured key into the root HTML. `frontend/src/apikey.ts:29-41` keeps it in JavaScript memory and sends it as a header. | Script execution, XSS, browser extensions, or an HTML disclosure can obtain the bearer secret. |
| WebSocket | `interface/routes/stream.py:60-72` accepts `?api_key=` and compares it before `accept()`. `frontend/src/feed.ts:357-365` constructs that URL. | A long-lived secret is placed in a URL and can enter browser history, proxy logs, telemetry, or referrers. |
| Live startup | `interface/fastapi_app.py:267-291` requires a key for live mode and permits only `127.0.0.1`, `::1`, or `localhost`; `:330-360` applies the check before startup. | The loopback gate is active, but the API key is not a production browser session. |
| Session/CSRF/revocation | No server-side browser session, CSRF contract, revocation registry, or key-migration mechanism is present in the inspected interfaces. | Do not infer production authentication from the existing API-key checks. |

A direct `uvicorn tradex_trading.interface.fastapi_app:serve_app` invocation cannot provide its actual bind host to the factory. The supported `tradex serve` and `start_fastapi_server` paths enforce the gate; the direct-factory limitation remains an operational caveat until the target deployment boundary removes the need for this interim workaround.

## 3. Goals and non-goals

### Goals

- Authenticate browser sessions without exposing a reusable credential to JavaScript.
- Use an opaque, server-side session referenced by a `Secure`, `HttpOnly` cookie.
- Prevent cross-site state changes with CSRF and origin checks.
- Authenticate WebSocket upgrades without putting a long-lived key in a URL.
- Make logout, compromise response, privilege changes, and key rotation effective across HTTP and WebSocket connections.
- Provide a bounded migration from the legacy API key without a silent authentication downgrade.
- Keep the live-bind restriction active until the target controls are deployed and verified.

### Non-goals

- Implementing the authentication design in this slice.
- Replacing broker authentication or the broker's own token/TOTP flow.
- Defining a new user directory, identity provider, MFA product, or account-recovery workflow.
- Making the chart read-only or changing order, replay, feed, or workspace behavior.
- Removing legacy files or changing the current live-bind implementation.

## 4. Threat model

### Assets

- Live order, cancel, modify, and position-management authority.
- Account, position, portfolio, workspace, and broker-account data.
- Session identifiers, CSRF secrets, API keys, and future signing/encryption key material.
- Market and replay data that must not be confused across users or sessions.
- Availability and integrity of the order path.

### Actors and trust boundaries

- A legitimate browser on the configured same origin.
- An authenticated operator using a second browser or device.
- An unauthenticated network client.
- A malicious website running in another origin.
- An attacker with access to a browser profile, extension, or the rendered HTML.
- A proxy, reverse proxy, log collector, or crash reporter that sees URLs and headers.
- A process or host that can read server environment variables or the session store.
- A former user whose session or credential has been revoked.

The browser, the server session store, the broker/session runtime, and all HTTP/WebSocket ingress points are separate trust boundaries. A client-side UI guard is never an authorization boundary; the server must enforce mode, account, replay, and session policy on every protected request.

### Threats and required controls

| Threat | Example | Required control |
| --- | --- | --- |
| Credential theft | XSS or an extension reads the injected API key from the DOM. | No reusable credential in HTML, JavaScript storage, or a URL; use an opaque HttpOnly cookie. Add a restrictive CSP and trusted-asset policy as defense in depth. |
| Cross-site request forgery | A hostile origin submits an order with the victim's ambient cookie. | Exact Origin/Referer validation plus a per-session CSRF token on every state-changing HTTP request; never rely on SameSite alone. |
| Session fixation or guessing | An attacker plants or brute-forces a session identifier. | Generate at least 256 bits of entropy, store only a verifier/hash at rest, rotate on login and privilege change, and rate-limit failures. |
| Cookie disclosure | TLS termination, logs, screenshots, or a compromised host exposes the cookie. | `Secure`, `HttpOnly`, `__Host-` cookie prefix, no Domain attribute, TLS everywhere, and redaction in all request telemetry. |
| Unauthorized WebSocket upgrade | A client opens `/ws/stream` without a valid identity or from an unapproved origin. | Authenticate before `accept()`, validate Origin, use the session cookie or a one-time ticket, and close unauthorized handshakes. |
| URL secret leakage | The current `?api_key=` value is recorded by a browser or proxy. | Remove the long-lived query credential; use a short-lived, single-use ticket only for an explicitly supported cross-origin flow. |
| Privilege or account confusion | A session retains live permissions after a role, broker account, or user change. | Bind authorization to a versioned server-side record and re-check it on each request and WebSocket message. |
| Replay and race attacks | A copied request is submitted twice or a logout races with a mutation. | One-time CSRF tokens, idempotency keys for order mutations, atomic session state transitions, and server-side replay/authorization checks. |
| Credential guessing | An attacker brute-forces login or the legacy key. | Rate limits, backoff, audit events, generic errors, lockout/alerting policy, and no keyless live startup. |
| Stale authorization | A revoked session keeps receiving data or can continue acting. | Shared revocation state, short revalidation intervals, active WebSocket closure, and an authorization epoch checked before privileged work. |
| Key rotation failure | A new key is deployed without a valid overlap or an old key is accepted forever. | Versioned key ring, explicit activation/expiry, bounded overlap, usage telemetry, atomic cutover, and fail-closed rollback. |
| Transport or proxy misconfiguration | A public listener, mixed content, or permissive CORS exposes an internal route. | Enforce the live bind gate, HTTPS, exact CORS origins, trusted proxy configuration, and deployment health checks. |

XSS remains capable of acting as the logged-in user even when the session cookie is HttpOnly. The session design limits credential reuse; it does not replace output escaping, dependency review, CSP, and a secure deployment.

## 5. Target authentication flow

1. The browser loads the same-origin UI without an embedded API key or session token.
2. `GET /auth/session` returns non-secret session metadata and, for an authenticated session, a CSRF token. It never returns the session identifier or CSRF secret used for server-side verification.
3. `POST /auth/login` authenticates through the selected operator identity source, applies the required second factor if configured, creates a fresh server-side session, rotates the session identifier, and sets the HttpOnly cookie.
4. Every protected HTTP request resolves the cookie through the server session store and attaches the resulting principal and authorization version to request state. Route handlers authorize the requested account and operation; they do not trust a client-supplied user, broker, or role.
5. `POST /auth/logout` atomically revokes the session, clears the cookie with the same attributes, and terminates or marks every associated WebSocket connection for closure.
6. A privilege, password, MFA, broker-account, or security-policy change increments the subject's authorization version. Existing sessions are rejected or re-authorized rather than silently retaining old privileges.

The exact identity-provider adapter and credential policy are deployment decisions, but the session, CSRF, WebSocket, and revocation contracts are not optional.

## 6. HttpOnly server-side session

### Cookie contract

The browser receives an opaque random session identifier in a cookie named with the `__Host-` prefix and the following attributes:

```text
__Host-tradex_session=<opaque random value>; Path=/; Secure; HttpOnly; SameSite=Lax
```

- `Secure` is mandatory for live and any non-local deployment. A loopback-only HTTP development server may use a separately named cookie without `Secure`; that exception is never used for a live bind.
- `HttpOnly` prevents JavaScript from reading the session identifier.
- The production `__Host-` name requires HTTPS, `Path=/`, and no `Domain` attribute, preventing subdomain cookie injection.
- `SameSite=Lax` is a browser hardening layer, not a replacement for CSRF validation.
- The identifier is generated from a cryptographically secure random source, is rotated on login, and contains no user, account, role, or expiry data.
- The cookie is never placed in HTML, a query string, a WebSocket URL, local storage, session storage, or application logs.

### Server record

The session store holds a verifier for the identifier rather than the raw identifier, together with:

- subject and broker-account identity;
- authorization and authentication version;
- creation time, last-seen time, absolute expiry, and idle expiry;
- CSRF secret verifier and rotation counter;
- device/session metadata needed for audit and abuse controls;
- revocation timestamp and revocation reason;
- the authentication method and key/credential generation used at login.

The store must be shared by all workers. Process-local session state is insufficient for a multi-worker deployment.

### Request handling

The authentication dependency must fail closed when the session store is unavailable, the cookie is malformed, the record is absent, the record is expired, or the subject is revoked. It must use constant-time verification where a verifier is compared and must not log the cookie or raw session identifier.

The UI may read a boolean authenticated state and display the user, but it cannot read or manufacture the session credential. The current `X-API-Key` verifier remains available only as a bounded migration path on the existing loopback development boundary; it is not a second production session mechanism.

## 7. CSRF protection

CSRF applies to every state-changing HTTP request authenticated by the session cookie, including order, cancel, modify, exit, workspace, and account mutations.

1. The server creates a cryptographically random CSRF secret when the session is created and stores only its verifier.
2. `GET /auth/session` returns the CSRF token to the same-origin UI in a response body. The token is not a bearer credential and is not stored in a URL or persistent browser storage.
3. The client sends the token in `X-CSRF-Token` on every unsafe HTTP method. The server compares it to the session record with constant-time comparison and rejects a missing, mismatched, expired, or rotated token.
4. The server additionally validates `Origin` against the exact configured UI origin and validates `Referer` when the browser supplies it. Missing or cross-origin values fail closed for unsafe methods.
5. CORS remains an exact-origin allowlist. A wildcard origin, wildcard credentials, or reflected untrusted origin is prohibited.
6. Login has its own login-CSRF token or equivalent pre-authentication origin check. Logout uses a CSRF-protected request unless the product explicitly chooses a narrowly scoped same-site logout exception.
7. Safe `GET`, `HEAD`, and `OPTIONS` requests must not perform mutations. Any endpoint that violates this rule is treated as a mutation and receives the full CSRF policy.

CSRF tokens are rotated on login, logout, session renewal, and authorization changes. A failed token is a security event, not a reason to retry automatically with a different credential.

## 8. WebSocket upgrade authentication

The current `?api_key=` mechanism is not retained for production. A browser WebSocket sends the same-origin HttpOnly cookie during the upgrade, so the server can authenticate without JavaScript access to a bearer secret.

### Same-origin upgrade

The `/ws/stream` handshake must, before calling `accept()`:

1. Validate the request method, path, TLS/proxy trust, and exact `Origin` allowlist.
2. Read the session cookie and resolve the server-side session, including expiry, revocation, authorization version, and account binding.
3. Reject an absent, expired, revoked, or unauthorized session with an HTTP authentication failure or a policy-specific close code; do not accept the socket first.
4. Bind the accepted connection to the immutable session ID and an authorization snapshot.
5. Authorize each subscription and privileged message against that binding. A connection cannot select another user's account or broaden its permissions through a frame.

On every message, and at a bounded heartbeat interval, the server rechecks revocation and authorization version. A revoked or stale connection is closed with code `1008` and is not allowed to reconnect using a cached frame or stale client state.

### Cross-origin or non-browser clients

If a separately deployed UI or trusted service needs a cross-origin WebSocket, the browser first obtains a one-time ticket from an authenticated, CSRF-protected HTTP endpoint. The ticket is:

- random, short-lived, single-use, and bound to the session, origin, and WebSocket path;
- stored only as a verifier server-side;
- redeemable only during the upgrade, never as a reusable API key;
- rejected on replay, origin mismatch, session revocation, or expiry.

The long-lived session cookie and any session identifier are not placed in the WebSocket URL. A native client should use a client certificate or an equivalent mutually authenticated mechanism where available; it must not fall back to a browser query-string key.

### Logging and reconnect behavior

Upgrade URLs, cookies, tickets, and authorization headers are redacted from access logs and traces. Reconnect logic requests a new ticket when required and does not assume that a previously accepted socket remains authorized. WebSocket authentication is independent of replay and feed authorization: a valid session still cannot submit an order while replay is active or when the session mode is not order-capable.

## 9. Revocation and session invalidation

Revocation is server-owned and applies to HTTP and WebSocket traffic together.

- Logout marks the session revoked and deletes or cryptographically invalidates its verifier atomically.
- Administrative revoke, password reset, MFA reset, suspected credential theft, account disablement, broker-account removal, and authorization-version changes revoke or invalidate affected sessions.
- A session ID, subject, device, or all sessions can be revoked by an authorized operator. Emergency revoke-all must not depend on a browser request succeeding.
- Absolute and idle expiry are enforced on every HTTP request and on the WebSocket heartbeat. Expiry is not satisfied by refreshing a cookie indefinitely.
- A shared store publishes revocation events or a monotonically increasing authorization epoch so every worker rejects the session promptly. A process-local cache may accelerate checks but cannot be the source of truth.
- Active sockets are closed when revocation is observed. A socket that cannot be closed immediately is denied further privileged messages and data frames.
- Login, logout, revocation, repeated authentication failure, key rotation, and authorization changes emit structured audit events without secrets.

A client that receives `401` or `403` clears local authenticated UI state and retries only through the normal login flow. It must not retry a mutation with a legacy API key or a cached WebSocket URL.

## 10. API-key and key-material migration

### Legacy credential to session credential

The current `TRADEX_SERVE_API_KEY` is a shared legacy bearer secret. It is injected into HTML, sent as `X-API-Key`, and accepted in the WebSocket query string. Migration must remove those browser delivery paths rather than adding sessions alongside them indefinitely.

Use these stages:

1. **Inventory and preparation.** Identify every caller, deployment, proxy, and test that uses the legacy key. Add metrics for authentication method and key ID without logging the value. Confirm the session store and revocation channel are available across all workers.
2. **Introduce the session path.** Add the session store, login/logout, CSRF, WebSocket upgrade authentication, and revocation behind an explicit deployment flag. The flag defaults off until the acceptance suite and an operator runbook pass.
3. **Bounded overlap.** If a controlled migration window requires the old key, accept it only through a dedicated compatibility verifier with a key ID and expiry. A successful compatibility login issues a session; the browser never receives the legacy key in HTML or a WebSocket URL. Restrict the compatibility path to the loopback development boundary or an explicitly approved migration host.
4. **Cut over.** Require sessions for protected HTTP routes and WebSocket upgrades, remove HTML injection and query-string authentication, and verify that legacy-key usage is zero outside the approved compatibility path. Do not silently fall back when the session store is unavailable.
5. **Retire.** After the rollback window, remove the compatibility verifier and environment variable, revoke the old key, and retain only versioned audit metadata. A later legacy client is a migration incident, not a reason to re-enable an unauthenticated path.

### Rotation and versioned key rings

- Every accepted legacy or bootstrap credential has a stable `kid`, creation time, retirement time, status, and usage counters.
- Rotation creates a new key, verifies distribution, switches the active verifier atomically, and permits a bounded overlap only when explicitly configured. The old key is revoked at the end of the overlap even if clients are still retrying.
- If encrypted session records or signed session metadata are introduced, their server-side key material uses the same versioned key-ring discipline. Key material is held in the deployment secret store, never in the repository, HTML, browser storage, or logs.
- Emergency compromise revokes the affected `kid`, all sessions issued under it, and any derived session-signing/encryption key in one coordinated operation.
- Missing, malformed, expired, or unknown key IDs fail closed. There is no environment-variable bypass and no fallback to keyless live mode.
- Rollback uses a known-good key within its bounded overlap; it never re-enables the old credential indefinitely.

Migration telemetry must distinguish session login, compatibility login, rejected key ID, session expiry, revocation, and CSRF failure. Values and raw tokens are excluded from metrics.

## 11. Active loopback-only live-bind gate

The interim gate is active now and remains mandatory while this design is unimplemented.

| Session mode | Bind host | Result |
| --- | --- | --- |
| `live` | `127.0.0.1`, `::1`, or `localhost` (case/whitespace normalized) | Allowed only with a non-empty API key. |
| `live` | `0.0.0.0`, `::`, a LAN address, or any other host | Startup fails with an actionable loopback error before Uvicorn binds. |
| `live` | Any host with no API key | Startup fails closed on the missing-key check. |
| `paper`, `backtest`, or `replay` | Existing deployment policy | The live-only bind check does not expand into a new restriction for these development modes. |

The supported CLI path calls the same checks in `interface/cli.py:370-415` and `interface/fastapi_app.py:330-360`. The factory path repeats the checks in `interface/fastapi_app.py:294-327`. There is no bypass variable. A reverse proxy or alternate launcher must not be used to evade the gate; public or non-loopback live serving is unsupported until the target authentication, CSRF, WebSocket, revocation, and migration controls are implemented and tested.

The direct `serve_app` factory limitation described in Section 2 is a known interim risk. Operators must use the supported serve command, restrict the process to loopback, and verify the actual listener with deployment controls. Removing the gate is a release decision, not a documentation-only change.

## 12. Implementation and acceptance sequence

This is the recommended order for a future implementation slice; none of these steps is performed by this document:

1. Add a shared session/revocation store and configuration schema.
2. Add login, session bootstrap, logout, and authorization-version endpoints.
3. Replace browser API-key delivery with the HttpOnly cookie while retaining the compatibility verifier only under the migration policy.
4. Add CSRF middleware and exact-origin enforcement to every unsafe HTTP route.
5. Change WebSocket upgrade authentication from query-string API key to session cookie or one-time ticket, with revocation checks.
6. Add key-ring rotation, compatibility telemetry, rollback, and emergency revoke procedures.
7. Add integration, browser, WebSocket, multi-worker, migration, and negative security tests.
8. Run a deployment rehearsal that proves a live non-loopback bind is rejected and a revoked session cannot use either HTTP or WebSocket.

The production release gate is all of the following: no key in rendered HTML or WebSocket URLs; cookie flags verified in a real browser; CSRF and origin tests pass; upgrade authentication rejects anonymous and cross-origin clients; revocation reaches every worker and active socket; migration overlap is bounded and observable; and the live-bind policy has an explicit, reviewed replacement before public binding is allowed.

## 13. Explicit status and follow-up

**This slice is documentation-only and the target authentication system is unimplemented.** In particular, this change does not add or modify:

- session storage or login/logout endpoints;
- HttpOnly cookie issuance;
- CSRF tokens or middleware;
- session-authenticated WebSocket upgrades;
- revocation, authorization epochs, or active-socket termination;
- API-key migration or key-ring rotation;
- production CORS, CSP, TLS, or proxy configuration;
- any production authentication or authorization behavior.

Recommended follow-up: implement the sequence in Section 12 in a separately scoped change, retain and test the loopback-only live-bind gate throughout, and keep legacy-key compatibility disabled outside the explicitly controlled migration window. Re-run the reference audit before any cleanup deletion; the present audit does not prove any target module is safe to remove.

## 14. Legacy-module reference audit

The audit was performed before this document was created, using tracked-file searches:

```text
git grep -n -E 'chart-lifecycle|chart-state|chart-types|shellbar|trade-bar' -- .
git grep -n -E 'chart-lifecycle\.ts|chart-state\.ts|chart-types\.ts|shellbar\.ts|trade-bar\.ts' -- .
git grep -n -E "from ['\"][^'\"]*(chart-lifecycle|chart-state|chart-types|shellbar|trade-bar)['\"]|import\(['\"][^'\"]*(chart-lifecycle|chart-state|chart-types|shellbar|trade-bar)['\"]\)" -- frontend trading openalgo-charts
```

Evidence returned:

- `frontend/src/chart-lifecycle.ts:12` imports `./chart-types`; `:13` and `:27` import `./chart-state`.
- `frontend/src/trade-bar.ts:3` imports `getOrderQty` from `./shellbar`.
- `docs/design/2026-09-13-frontend-integration-map.md:69` and `:93` mention `trade-bar.ts` and its order-quantity role.
- Other matches were documentation or test wording, not proof that a module is unused.
- `git ls-files --error-unmatch` confirmed all five requested files are tracked.

No direct external tracked importer was found for `chart-lifecycle.ts`, `trade-bar.ts`, or `shellbar.ts`, and no deletion was performed. `chart-state.ts` and `chart-types.ts` have live internal references from `chart-lifecycle.ts`; `shellbar.ts` has a live internal reference from `trade-bar.ts`. The absence of a static importer for a remaining file is not conclusive evidence against dynamic, generated, packaging, or future integration use. All five files remain untouched under the “do not delete unless conclusively absent” rule.
