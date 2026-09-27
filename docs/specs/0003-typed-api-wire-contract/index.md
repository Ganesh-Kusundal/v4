# 0003. One typed contract for the HTTP and WebSocket API

**Date**: 2026-09-27
**Status**: Accepted

## Summary

The frontend (TypeScript) and the backend (Python) talk to each other over a wire:
the HTTP API under `/api` and the WebSocket stream at `/ws/stream`. Right now
nothing types that wire. Every chart endpoint takes a bare `dict` body and
hand-parses it, and the frontend hand-writes its own TypeScript interfaces next
to it. The two sides are never checked against each other, so they drift apart
quietly and the drift only shows up as a wrong number on screen. This spec makes
one source of truth for the wire: Pydantic request and response models on every
route, the WebSocket frames built from models too, and the frontend's types
generated from the resulting OpenAPI schema and checked in CI so a change on one
side that the other side has not absorbed fails the build. It is deliberately
narrow: it types the wire, it does not move any business logic, and it is not the
internal command and event envelope work that was rejected earlier.

## Context

This spec standardises how the HTTP and WebSocket boundary is typed across the
whole repository. It came out of a review of where business logic actually lives
on each side of that boundary, and of the specific defects that a boundary with
no types produces.

**The current state, concretely.** The Python side already has a module for this,
`interfaces/src/tradex_interfaces/models.py`, holding a handful of Pydantic
response models (health, account, position, order, and a stable error envelope).
That module is the right idea and the right home, but it is barely used. The
chart router, which is the largest surface the frontend consumes, does not use it
at all. Instead every chart endpoint follows the same pattern: it takes
`body: dict`, reads fields with `body.get(...)`, and declares `-> dict` as its
return type. On the TypeScript side, each module that calls the API declares its
own interfaces by hand. Nothing generates one side from the other, and no test
compares them.

**The competing patterns, and what each costs.**

1. **A bare `dict` body and a `-> dict` return** (the chart router, most of the
   surface). FastAPI cannot validate the body, cannot reject a wrong field type
   with a clear error, and emits an OpenAPI schema that says almost nothing. The
   frontend is free to send a field the backend ignores, and the backend is free
   to omit a field the frontend reads.
2. **Pydantic response models with no request model** (the health, account,
   order, and portfolio routes, via `models.py`). The response side is typed and
   documented, but the request side is still a `dict` where one exists at all.
   Better, and the direction to grow.
3. **Hand-written TypeScript interfaces per calling module** (everywhere in
   `frontend/src`). Each is a private copy. They drift from the backend
   independently, and nothing fails when they do.

**What breaks when these coexist.** The drift is not theoretical. Concretely
verified on the current tree:

- A metric named `num_trades` counts filled orders, not round trips, while its
  own docstring says it counts signals. One run reported 55 and 27 for the same
  thing, because the metrics panel and the trades table read the same number
  from two different definitions.
- The history endpoint returns one set of keys on a cache hit (it drops `source`
  and `timeframe_resampled`) and a different set on a cache miss, so a client
  that reads `source` gets a different response shape depending on whether the
  server happens to have served that request recently.
- The daily interval is spelled `D` in one place, `1d` in another, and `1D` and
  `d` are each accepted somewhere, across the REST route, the WebSocket route,
  and the TypeScript side.
- The backtest endpoint reads a `fees` flag out of the request body, letting a
  caller switch costs off in a run whose numbers are compared against a
  promotion bar.

None of these raise. Each is a UI number or a UI field that is locally
self-consistent, so nothing in the test suite fails, while it means something
other than what it says. That is the specific failure this standard exists to
prevent.

**Scope boundary.** This spec is about the wire between the two processes. It is
not the internal typed command and event envelope work proposed in an earlier
plan and deliberately dropped (the review found that module was dead code). That
was in-engine envelopes around order commands; this is the HTTP and WebSocket
boundary. The two do not overlap. This spec also does not move any business
logic, does not change where indicators or strategies are computed, and does not
restructure any package.

## Decision

Every HTTP request body and every HTTP and WebSocket response is defined once, as
a Pydantic model, in `tradex_interfaces/models.py`. The routes use those models
for both validation and response shape. The WebSocket frames are Pydantic models
built by `stream.py`. One codegen step turns the resulting OpenAPI schema into
TypeScript types checked into `frontend/src/api/schema.ts`, and CI regenerates
and diffs so a backend change the frontend has not absorbed fails the build.

**Implementation skills**: none installed and required for this decision. The
project has no `AGENTS.md`; `CLAUDE.md` is the context file and it already records
the render only rule (the chart renders, the backend computes) that this contract
protects.

## Rationale

Reasoning and options: see [rationale.md](rationale.md).

## Standard definition

**Canonical pattern.** A route declares Pydantic request and response models and
uses them. Nothing hand-parses a `dict`, and nothing returns a bare `dict`.

```python
# interfaces/src/tradex_interfaces/models.py
class IndicatorComputeRequest(BaseModel):
    exchange: str = "NSE"
    symbol: str
    interval: str = "5m"
    from_: int | None = Field(default=None, alias="from")
    to: int | None = None
    id: str
    params: dict[str, Any] = Field(default_factory=dict)

class IndicatorPoint(BaseModel):
    time: int
    # one optional float per plot, plus the "value" single-plot shorthand
    model_config = ConfigDict(extra="allow")

class IndicatorComputeResponse(BaseModel):
    id: str
    points: list[IndicatorPoint]
    meta: dict[str, Any]
```

```python
# interfaces/src/tradex_interfaces/routes/chart.py
@router.post("/indicators/compute", dependencies=[Depends(require_auth)])
async def compute_indicator_endpoint(
    body: IndicatorComputeRequest,
) -> IndicatorComputeResponse:
    # validated by FastAPI before the body is touched; no body.get(...) anywhere
    ...
```

The WebSocket side follows the same rule. A frame is a model, built by the route
and parsed by the frontend from a generated type.

```python
# the frame models live in models.py too
class BarFrame(BaseModel):
    type: Literal["bar"] = "bar"
    instrument: str
    interval: str
    time: int
    open: float
    high: float
    low: float
    close: float
    volume: float
    closed: bool
    source: str
    run_id: str | None = None
    provisional: bool = False
```

The frontend imports the generated types and never redeclares a wire shape.

```ts
// frontend/src/api/schema.ts   (generated, checked in, never hand edited)
export type paths = { ... };
export type components = { schemas: { IndicatorComputeRequest: ... } };

// a calling module imports the type, it does not copy it
import type { components } from "./api/schema";
type HistoryResponse = components["schemas"]["HistoryResponse"];
```

**Replaces.** These patterns are now wrong:

- A route handler that takes `body: dict` and reads it with `body.get(...)`.
- A route handler that declares `-> dict` as its response type.
- A hand-written TypeScript interface in `frontend/src` that describes a wire
  shape (a copy of a backend field set).
- Building a WebSocket frame as an ad hoc dict literal in `stream.py`.

**Enforcement.** Compile time and build time, in two places.

- On the Python side, typing the handler signature is the enforcement. A handler
  that takes `dict` is not a type error today, so a lint check forbids it. The
  check is a small AST rule (an importable helper, not a plugin) added to the
  existing quality gate: a route handler under `tradex_interfaces/routes/` whose
  body parameter is annotated `dict`, or whose return annotation is `dict`, fails
  the gate. It is the same shape as the existing import boundary gate.
- On the TypeScript side, the generated schema is checked in and CI regenerates
  it and fails on any diff. A backend model change that is not regenerated, or a
  hand edit to `schema.ts`, is a red build.

**Rollout.** Gradual, chart first, with each step landing whole.

1. The chart router under `/api/charts/*` and the WebSocket frame models, which
   is where the reviewed drift lives (indicators, history, backtest, scanner,
   seasonality, transforms, profiles).
2. The rest of the HTTP routers (health, account, portfolio, market_data,
   extensions) and the orders router.

New routes follow the standard from the moment they are written. Existing routes
are migrated router by router, and the lint rule starts failing on them only
after the migration for that router lands, so the gate never blocks work in
progress. Orders and the money path are migrated last and only after spec 0002
Wave 1 and Wave 2 have landed, so this work does not collide with them.

**Exceptions.**

- The error envelope is already a model and stays one. `ErrorResponse` is the
  single error shape for the whole API.
- `/health/live` and `/health/ready`, and the `/metrics` Prometheus text, are not
  JSON API responses and are out of scope.
- The static `/ui` mount is not part of the API surface.
- A response that is genuinely open ended, the indicator catalogue and the
  seasonality table among them, uses a model with a documented inner shape plus
  an open map, not a bare `dict`, so the outer contract is still typed.

## Feature design

**The contract surface.** One module, `tradex_interfaces/models.py`, holds every
model. It already exists and already holds the error envelope and a few response
models, so this grows it rather than creating a parallel module. Models are
grouped by the router that owns them, and each router imports its own.

**Request models.** One per endpoint that has a body, named
`<Thing>Request`. The indicator, transform, profile, seasonality, backtest, and
scanner bodies become models. Field names use the wire names, including the
existing `from` alias, so the OpenAPI schema and the wire agree.

**Response models.** One per endpoint, named `<Thing>Response`. Where a response
carries bars, a `Bar` model is the single definition of a bar (`time`, `open`,
`high`, `low`, `close`, `volume`) and both the history response and the WebSocket
bar frame reuse it, so a bar is defined once.

**WebSocket frames.** Every frame type the stream emits and every message type it
accepts becomes a Pydantic model: the bar, depth, quote, indicator, fill,
position, and order frames on the way out; `subscribe`, `unsubscribe`,
`subscribe_bars`, `indicator-sub`, and the replay control messages on the way in.
`stream.py` builds frames by constructing the model and serialising it, rather
than by a dict literal, so a field rename is a type error in Python instead of a
silent wire change.

**The generation step.** The FastAPI app already serves an OpenAPI document. One
build script runs `openapi-typescript` against that document and writes
`frontend/src/api/schema.ts`. The script is idempotent and is the only thing that
writes that file. CI runs the same script and fails if the file changes, which is
the drift alarm. The WebSocket frame models are exposed in the same schema, under
`components/schemas`, so the frontend gets them from the same generated file.

**The frontend migration.** Each calling module replaces its hand-written wire
interfaces with imports from the generated schema. The five verified defects are
fixed as part of this, and they are contract fixes, not logic changes:

- `num_trades` is renamed at the source to say what it counts, and the trades
  table and the metrics panel read one definition. (The counting semantics
  themselves are spec 0002 lane 1l territory; this spec only stops the two
  surfaces from disagreeing about a name.)
- The history response returns the same keys on a cache hit and a cache miss, so
  the response shape does not depend on cache state.
- The daily interval has one canonical spelling on the wire, with the existing
  aliases accepted at the edge and normalised before anything downstream reads it.
- The `fees` switch is removed from the backtest request body, matching spec 0002
  lane 1j, so a caller cannot turn costs off in a run that is compared against the
  promotion bar.

**Value sourcing.** Every value the contract produces has one named source, so
the build never invents one.

| Value on the wire | Source |
| --- | --- |
| bar `time` | UTC seconds of IST wall clock, converted once in `_serialize_series` (existing) |
| bar OHLCV | the datalake store, with the broker fallback (existing) |
| indicator point values | `tradex_analytics.indicators.compute_indicator` over the same bar window (existing) |
| backtest metrics and fills | `tradex_replay.BacktestEngine.run` (existing) |
| scanner candidates and scores | `tradex_strategy.core.scanner.ScannerEngine` (existing) |
| strategy and scanner catalogue | `tradex_strategy.extensions` discovery (existing) |
| account and book figures | the session's engine and broker projection (existing) |
| error `code` and `message` | the existing `ErrorResponse` envelope and status code map (existing) |
| every request and response field name and type | the Pydantic model in `models.py`, which is the single source |

**Security model.** This spec changes no authentication or authorisation. The
`require_auth` dependency stays on every route that has it today, and the
workspace routes stay deliberately unauthenticated (loopback only, local layout
state; the reasoning is recorded in the route comment). Typing a body does not
replace authorisation, and no model in this spec grants access. One security
note: because bodies become validated, a request that used to be silently
accepted with a wrong field type will now be rejected with a 422. That is the
point, and it is a visible behaviour change for any caller that was sending a
malformed body and relying on it being ignored.

**Edge cases and failure modes.**

- A request with an unknown field. Pydantic models ignore unknown fields by
  default. The decision is to ignore them on the way in (so a newer frontend
  against an older backend does not break) and to have the generated types still
  name the fields, so the TypeScript side catches the mismatch at build time
  instead.
- A response field that is sometimes absent. Optional fields are modelled as
  `T | None` and are present in the schema, so the frontend must handle the
  absent case explicitly rather than reading a field that may not be there.
- The generated file is out of date. The CI diff fails, naming the file and the
  command to regenerate. This is the intended alarm, not a nuisance to suppress.
- A backend model change that is intentionally not yet in the frontend. The
  generated file is regenerated in the same change, and the frontend is updated
  to match, or the change is split. The gate does not allow a model to change
  without the generated file changing with it.
- The bar model is shared by REST and WebSocket. A change to a bar field is a
  change to both surfaces at once, which is intended, and it is why the bar is
  defined once.

## Proposed stack

The wire is typed with Pydantic, which FastAPI already uses and which is already
a project dependency through the `models.py` module and the error envelope. There
is no new Python library.

The TypeScript types are generated by `openapi-typescript`, run as a small Node
build script, added to `frontend/package.json` as a dev dependency and a
`generate:api` script. It is the standard, boring choice for turning an OpenAPI
document into TypeScript types, it needs no running backend at build time once the
schema is exported, and it keeps the generated file small and readable. The runner
up is a hand written generator script, which was rejected because it would be
something to maintain and would drift from the OpenAPI document it reads.

The WebSocket frames are modelled with Pydantic like the REST bodies, and exposed
in the same OpenAPI document so they arrive in the same generated file. No
separate WebSocket schema tool is introduced.

## Consequences

**What this fixes.** The four verified drifts become impossible to reintroduce
silently: a backend change that the frontend has not absorbed fails CI; a handler
that reverts to a bare `dict` fails lint; a hand written wire interface in the
frontend is no longer needed and its copies go away; and the two response shape
bugs and the interval spelling are fixed as part of the migration.

**What this costs.** Every route in the chart router, and eventually every route
in the app, gains request and response models, which is real work and makes the
chart routes longer, because the models move out of the handlers. The build gains
a generation step and a CI check, so a backend model change now costs a
regeneration. The generated file is checked in, so it is a file a developer can
edit by mistake, and the CI diff is what catches that.

**What stays the same.** No business logic moves. The backend still computes every
indicator, every backtest, and every scan; the frontend still only renders. No
package boundary changes, no new Python dependency, and the authentication model
is untouched.

**What is deliberately not done.** The internal command and event envelope work
is not revived here, and the orders and money path are left until spec 0002 Wave
1 and Wave 2 land, so the two efforts do not overlap on the same routes.

## Follow-up

- [ ] Decide where the `openapi-typescript` conventions belong. There is no
      `AGENTS.md` in this repository; `CLAUDE.md` is the context file. The rule
      that a wire shape is defined once in `models.py` and never copied into
      `frontend` belongs in `CLAUDE.md` next to the render only rule it protects,
      which is the one-line pointer approach rather than a new context file.
- [ ] The lint rule that forbids `dict` bodies and `dict` returns on route
      handlers is a small AST check. Decide whether it lives beside the existing
      import boundary gate in the quality gate, so the two structural checks are
      read together.
- [ ] The WebSocket frame models move a real number of dict literals in
      `stream.py` into model construction. That is the largest single edit in this
      spec and is worth doing as its own change, separate from the REST models,
      so a review of it is not mixed with a review of the REST surface.
- [ ] After the chart router is migrated, re-run the drift review that produced
      this spec and confirm the four defects are gone, as the negative control
      that the gate would have caught them.

Reasoning and options: see [rationale.md](rationale.md).
