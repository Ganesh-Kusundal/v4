# Audits, September 2026

Three generations of self review, in the order they were written. The newest supersedes the two
before it, and all three are superseded by the programme spec. They are kept here rather than at
the repository root so that exactly one document is current.

The current plan is [0002 production readiness programme](../../specs/0002-production-readiness-programme/index.md).
Its decision record, including where each idea in these three documents ended up and which parts
were rejected, is [rationale.md](../../specs/0002-production-readiness-programme/rationale.md).

| Document | Pinned at | What it was |
|---|---|---|
| [2026-09-26-predeploy-review.md](2026-09-26-predeploy-review.md) | `df377a0` | First pass. Predicted trouble before deploying |
| [2026-09-26-predeploy-plan.md](2026-09-26-predeploy-plan.md) | `df377a0` | Plan to act on it. Carries a superseded in part banner, and a 19 line uncommitted correction on top |
| [2026-09-26-deep-review-findings.md](2026-09-26-deep-review-findings.md) | `df377a0` | Addendum. Found three of its own prior conclusions were wrong, two of which would have caused damage |

## Status of the deep review's Critical findings

All five are **closed**. The deep review ran on 2026-09-26 and the eight commits between
`fcaa6b4` and `b941da2` fixed each one. This is recorded here so nobody re reads that document and
re opens work that is done.

| Finding | Closed by | Verified at HEAD |
|---|---|---|
| M-1, unguarded read modify write on the position book from mark to market, shares vanishing | `a2e0371` | `mark_to_market.py:91` takes `position_lock` |
| M-2, `submission_boundary_crossed` undeclared and defaulting false, a second live order under one key | `709274c` | `fill_sources.py:56` declares it on the Protocol |
| M-3, idempotency key completed before money moved, fee permanently lost, replay diverging | `5f3e4b7` | `engine.py:972` records the result after the mutations (was `:847` at the time of the fix; the file has grown since — the anchor is the `M-3` comment block in `_apply_fill`) |
| A committed fill destroyed by a concurrent re mark | `f37749e` | same region |
| SIGTERM skipping session teardown | `a794fbe` | `fastapi_app.py:104-112` documents the lifespan ownership |

The one related finding that is **not** closed is the unlocked cash ledger, raised separately by
the 13 panel review and scheduled as lane 2h. Note the distinction the deep review drew and got
right: the cash ledger *arithmetic* is correct and should not be re litigated. Its *thread safety*
is a separate defect, and that one is real.

## Findings still open, and who owns each

The open set is exactly what 0002 does not schedule. Nothing here is forgotten; it is recorded so
the boundary between funded and unfunded work is visible.

**Owned by 0002.** Everything else in the 13 panel review
([2026-09-27-full-platform-review.md](../../audit/2026-09-27-full-platform-review.md)), mapped
wave by wave in the spec's rationale.

**Not scheduled by 0002, still open:**

| Finding | Severity | Why not scheduled |
|---|---|---|
| Path traversal, a caller supplied symbol interpolated into the parquet path | Medium | Bounded to a parquet shaped read. Belongs with the account and multi tenant work |
| Environment root variables accepted unvalidated, including the token state directory | Medium | One operator machine. Validate at boot in the operational wave if it ever moves |
| Session cookie `secure=False` with a `ponytail:` deferral comment | Medium | Loopback HTTP today |
| Raw `str(exc)` returned to the client on about ten sites | Medium | Fix alongside the typed error envelope work, not before it |
| No dependency scanning, every GitHub Action on a floating tag | High | Should be done. It is an hour of work and 0002 does not have a lane for it. Cheapest unowned risk in the repo |
| `openalgo-charts` supply chain, a gitignored clone whose `dist/` is the golden fixture ground truth | High | The pin is CI verified. A `dist` content hash in the build stamp would close it |
| Frontend workspace boot timeout overwrite, a slow load silently replaces a saved layout with a blank chart | High | Data loss but not money. Belongs with the frontend work in a later wave |
| Frontend has no global error boundary, so any boot throw leaves a blank page | Medium | Same |
| Frontend API types are hand written, so a backend rename shows a dash instead of failing | Medium | Generate from OpenAPI. Same wave as the frontend work |
| Event log is `pickle`, which is remote code execution against a local file | High | 0002 lane 4c makes snapshots canonical JSON but does not migrate the log itself. The most significant item on this list |
| Per symbol and per sector concentration limits | High | Real money only, wave 6 |
| Multi account and sub account authorisation | High | One account is the stated target |
| A read model and query projections | Medium | Every interface read hits the write model. Does not scale past one account, and one account is the target |
| Formal pre trade risk engine and a pre open checklist | High | The `RiskManager` has primitives and no policy. Wave 6 |
| Multi process and socket sharding | Deferred | The 22,661 ticks per second ceiling is not binding for one strategy. Wave 7 |
| The full market data tape store | Deferred | Lane 2g builds the smallest version that unblocks the paper path. Wave 7 |

The two rows worth acting on outside the programme are the missing dependency scanning, because it
is an hour of work with no dependencies, and the `pickle` event log, because it is the only
remaining remote code execution path in a process that holds live broker tokens.
