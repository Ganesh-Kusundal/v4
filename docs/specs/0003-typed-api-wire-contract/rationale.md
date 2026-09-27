# 0003. One typed contract for the HTTP and WebSocket API, rationale

## Context

This is the decision record for spec `0003-typed-api-wire-contract`. The build
spec is [index.md](index.md). This file holds the reasoning, the options that
were weighed, and the review evidence the decision rests on.

The decision came out of a review of where business logic lives on each side of
the frontend and backend boundary, and of the concrete defects that a boundary
with no types produces. The review's finding, in one line: the render only rule
(the chart renders, the backend computes) is architecturally sound and mostly
upheld, the indicator and backtest computation is genuinely on the Python side,
but the frontend has become the authority on what a backtest run held and what it
made, and the wire between the two sides is untyped, so the two sides can disagree
about a number or a field with nothing failing.

## Options considered

### Option 1: Pydantic request and response models on the routes, TypeScript types generated from the OpenAPI schema, checked in CI

Describe the wire once, as Pydantic models in `tradex_interfaces/models.py`, on
both the request and the response side of every route and every WebSocket frame.
Generate the frontend's TypeScript types from the resulting OpenAPI document with
`openapi-typescript`, check the generated file in, and fail CI when it drifts.

This is the chosen option. Its strength is that it makes the divergence the
review found structurally impossible to reintroduce quietly: a backend model
change that the frontend has not absorbed is a red build, not a wrong number. It
reuses Pydantic, which the project already uses, and it needs no new Python
library. Its cost is real work on every route and a new build step.

### Option 2: Pydantic models, generated types, but no CI drift check

The same models, but the generated file is refreshed when someone remembers to
run the generator.

The drawback is the one that matters. Without the check, the generated file is
just another hand maintained file, and it goes stale in exactly the quiet way the
current hand written interfaces do. It removes the typing effort without removing
the drift, which is the problem being solved.

### Option 3: Hand maintained TypeScript types, kept in sync by convention and review

Keep the interfaces in `frontend/src`, add Pydantic on the backend for
documentation, and rely on review to keep them aligned.

This is what the project does today, distributed across modules, and the review
found four live defects that it did not catch. It is the cheapest to start and
the most expensive to keep, because nothing enforces the alignment and every
reviewer has to remember to check two files in two languages for one field set.

## Rationale

Option 1 is the right choice, and the deciding force is the specific failure the
review documented. The defects are not type errors. They are numbers and fields
that are locally self consistent, so no test fails, while they mean something
other than what their name says. A metric called `num_trades` counted filled
orders while its own docstring said it counted signals, and one run reported 55
in the metrics panel and 27 in the trades table for the same thing. The history
endpoint returned one set of keys on a cache hit and a different set on a cache
miss, so the response shape depended on server cache state. The daily interval was
spelled three ways across the REST route, the WebSocket route, and the frontend.
None of these are the kind of bug a reviewer spots by reading one file. They are
the kind of bug a single typed contract makes impossible.

The engineer was offered a fourth path and took the recommended one on every
dimension: the chart router and the WebSocket frames first, because that is where
the reviewed drift lives; orders and the money path last, and only after spec
0002 Wave 1 and Wave 2 land, so the two efforts do not collide on the same routes.

Two boundaries were drawn on purpose. First, this is the HTTP and WebSocket wire,
not the internal command and event envelopes. An earlier plan proposed typed
envelopes around order commands and the review deliberately dropped that, because
the module it proposed was dead code. That work was in the engine; this work is
at the process boundary. They do not overlap, and reviving the dropped idea here
would be scope creep against a decision that was already made. Second, this moves
no business logic. Typing the wire is a change to how the two sides agree on
shape, not to where anything is computed, and the render only rule is unchanged.

The WebSocket frames are included rather than deferred because they are the same
class of problem on the same boundary, and the stream builds its frames as ad hoc
dict literals that the frontend then hand parses. Leaving the socket untyped while
typing REST would leave the more stateful, more order sensitive half of the
boundary exactly as untyped as it is today.

The frontend is migrated by importing the generated types, not by adopting a new
runtime data layer. Adding a client library on top of FastAPI would be a larger
change than the problem needs, and the frontend already has a thin, working fetch
and socket layer in `feed.ts` that the generated types fit into without being
replaced.

## Evidence

The following were verified directly on the tree at commit `b941da2` and are the
basis for the defects this spec fixes.

- The chart router takes `body: dict` and returns `-> dict` on the indicator,
  transform, profile, seasonality, backtest, and scanner routes
  (`interfaces/src/tradex_interfaces/routes/chart.py`).
- `num_trades` counts filled orders (`replay/src/tradex_replay/backtest.py`), and
  the metrics panel and the trades table read two different definitions of it
  (`frontend/src/tier2.ts`, `frontend/src/backtest.ts`).
- The history response returns a different key set on a cache hit than on a cache
  miss (`chart.py`, the cache hit branch omits `source` and `timeframe_resampled`).
- The daily interval is spelled `D` on the REST route, `1d` on the WebSocket
  route, and both `1D` and `d` are accepted as aliases.
- The backtest reads a `fees` switch out of the request body.
- The frontend computes round trip pairing, per trade profit and loss, and a
  drawdown column locally, none of which the backend ships
  (`frontend/src/backtest.ts`), and the trades panel re sums them
  (`frontend/src/trades-panel.ts`).
- Six frontend modules are not reachable from the entry point, and one of them
  carries a local profit and loss that contradicts the backend's, and a fail open
  duplicate of the stop side check. Spec 0002 lane 4d already schedules their
  deletion, so this spec does not schedule it again.
- `tradex_interfaces/models.py` already exists and already holds the error
  envelope and a few response models, and is the natural home for the rest.

## What this spec deliberately does not decide

- Where the business logic lives. That is settled by the render only rule and is
  not changed here. The local profit and loss and round trip pairing in the
  frontend are a real boundary problem, but moving them onto the backend is
  spec 0002's parity work (lane 1l), not a typing change, and bundling it here
  would make this spec a logic change.
- The authentication model. Unchanged. The workspace routes are deliberately
  unauthenticated and that decision is recorded in the route.
- Any package restructure. Spec 0002 Wave 4 owns that.

## References

Project sources:

- `interfaces/src/tradex_interfaces/models.py`, the existing response models and
  the error envelope, the home this spec grows.
- `interfaces/src/tradex_interfaces/routes/chart.py`, the routes this spec types.
- `interfaces/src/tradex_interfaces/routes/stream.py`, the WebSocket frames.
- `replay/src/tradex_replay/backtest.py`, the `num_trades` definition.
- `frontend/src/backtest.ts`, `frontend/src/trades-panel.ts`, the local profit
  and loss that a typed contract does not fix and spec 0002 lane 1l owns.
- `docs/specs/0002-production-readiness-programme/index.md`, the accepted
  programme this spec defers to on orders, the money path, and the dead
  frontend.
- `docs/deep-refactor-plan.md`, phase 3, the typed envelope work that was
  deliberately dropped and that this spec is not reviving.
- `CLAUDE.md`, the render only rule and the chart contract this spec protects.
