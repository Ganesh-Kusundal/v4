# Design: Production Readiness Fix Programme

**Date** 2026-09-29 · **Status** approved in sections · **Pinned at** `c01fba5`
**Source** `docs/reviews/principal-engineering-review-2026-09-29.md`

## Objective

Close the findings from the principal engineering review, starting with the six
CRITICAL defects that block a production deployment. Fix the four HIGH findings
that are correctness rather than judgement. Then do the test-architecture and
deletion work that the earlier waves make possible.

The shape of this programme is mostly **wiring work that was never finished**,
not new construction. That is deliberate and it is the cheapest possible route
to a correct system.

## Decisions taken during design

Two findings were judgement calls rather than fixes, and both were decided on
evidence rather than preference.

**H1, snapshots: implement the fold, do not delete the feature.** The event log
has no pruning — a repo-wide search for `VACUUM`, `DELETE FROM events`, or any
retention policy returns nothing — so it grows without bound, and
`recover_trading_cache` replays all of it from zero on every restart
(`recovery.py:335`). Restart time therefore grows linearly with uptime, forever.
The snapshot machinery already exists, already stores canonical JSON, and
already has a tested decoder; it simply has no reader. Deleting it would discard
the exact component that makes the Phase 2 JSON migration cheap.

**H2, fill dedup: back it with the store, do not delete it.** The engine uses an
in-memory LRU (`idempotency.py:122`) that dies with the process. The durable
`fill_seen`/`record_fill` exists at `sqlite_event_store.py:1130-1148` with zero
production callers. A broker resending a fill after a restart double-applies it.
That is a real-money defect, not dead code.

**C3, pickle: ship a restricted `Unpickler` now, schedule the JSON migration.**
The allowlist is roughly fifteen lines and closes the RCE immediately. The
migration to a v3 JSON decoder is the durable fix and is Phase 3 work.

## Scope

All seven roadmap phases. Phase 3 and Phase 4 are in scope; Phase 7's package
deletions (L4) are deferred to Wave 3 pending verification that nothing imports
them.

## Wave 1 — four lanes, disjoint file ownership

No lane edits `startup.py` or `config/schema.py`. `startup.py` is the
composition root and imports every package; four agents editing it in
parallel produces silently reverted lines. Registration is Wave 2's job.

`engine.py` is excluded for a different reason: it has uncommitted work in the
working tree (see Blockers). Lane H therefore delivers the *durable dedup
component* against `idempotency.py`, and Wave 2 performs the single
`engine.py` edit that binds it.

| Lane | Owns exclusively | Delivers |
|---|---|---|
| **B** security | `sqlite_event_store.py`, `sqlite_store.py` | C3 restricted `Unpickler`; H3 `PRAGMA synchronous=FULL` |
| **C** paper + credentials | `paper/adapter.py`, `token_lifecycle.py` | C5 order-book default, margin gate, calendar gate; C6 chmod + boot assertion |
| **H** durability | `recovery.py`, `idempotency.py` | H1 snapshot fold; H2 durable fill dedup **component** (the `engine.py` binding is Wave 2) |
| **A** strategy | `bar_aggregator.py`, `sizing.py` | C1 aggregator component; C2 sizer factory — as injectable units, not yet wired |

B and H both work inside `execution/` but own different files. Cross-imports are
read-only and therefore safe.

Each lane delivers a tested component plus a short "how to wire" note. A's
aggregator and sizer are not registered until Wave 2.

## Wave 2 — serial composition-root wiring

One agent wires all four registrations into `startup.py` in a single sitting,
after Wave 1 has landed. This cannot be parallelised and is serialised honestly
rather than pretended otherwise.

It also owns `config/schema.py` for C4 (default `persistence.path` in live mode,
refuse the boot when unset) and the two `engine.py` edits: H2's binding of the
durable dedup, and H4 (route all money-moving broker mutations through
`submit_mutation`).

## Wave 3 — serial test architecture and deletion

Phase 6 and Phase 7. Cannot run earlier: deleting 114 assertion-free tests and
parametrising 116 duplicate groups would collide with the tests Wave 1 lanes are
writing.

- Parametrise the 116 duplicate groups; delete the 114 assertion-free tests.
- Add `@pytest.mark.unit/integration/slow` so a fast tier exists.
- Replace 58 unpatched `time.sleep` calls with `threading.Event` waits.
- Delete `DurableTokenManager`, the `auth.__getattr__` shim, the empty
  `upstox/_alerts.py`; untrack `brokers/.venv` and stale `.pyc`.
- Delete the stale `tradex-trading` packaging metadata.
- L4 package deletions only after verifying no importer exists.

## Blockers

**Your uncommitted `engine.py` work blocks the `engine.py` edits in Wave 2.**
C4, H2's binding and H4 all need that file, which currently has open changes (a
`record=False` flag on `reconcile()`). No agent will edit a file with uncommitted
changes in it — that is how the earlier clobber happened. Wave 1 is unaffected:
it touches no `engine.py`. Either commit the work before Wave 2, or those three
items move to Wave 3.

## Gates

**Gate 0, before any lane starts.** Baseline green from the repo root:
`.venv/bin/python -m pytest -q --timeout=600 -p no:randomly`. A specialist
confirmed 5,626 passed at `c01fba5`, and the `trading/src` conftest insert is a
no-op because that directory is empty, so the stale-import-shadow trap that
produced a false "pre-existing failures" verdict earlier in this repository does
not apply. A non-green baseline stops the programme and gets diagnosed, not
absorbed by four agents simultaneously.

**Negative controls, one per behaviour.** This repository already holds this
standard; lanes 3i, 4c and 5e were built to it. Each control fails when its fix
is removed.

| Lane | Control | Fails today because |
|---|---|---|
| B | temp DB with an attacker `__reduce__`; `replay()` must not execute it | payload is pickle |
| B | assert `PRAGMA synchronous` is `FULL`; neuter to `OFF` | no pragma set |
| C | drive cash negative; assert refusal | order fills, cash goes to −9,000 |
| C | with defaults, a resting order partially fills across two levels | `order_book=False` |
| H | 10,000 events + snapshot + 100 more; recovery touches only 100 | replays 10,100 |
| H | record a fill, restart, replay it; assert not double-applied | LRU empty after restart |
| A | ticks through the real boot; assert `on_bar` fires | no aggregator wired |
| A | assert backtest-sized == live-sized for one signal | 8,333 vs 7,916 |

**Per-lane gate before merge.** `ruff` clean on touched files; the lane's own
suite green; every control demonstrated to fail when its fix is reverted. The
coordinator runs each mutation rather than accepting the lane's word — that
standard already caught one of the coordinator's own tests pinning nothing.

**Per-integration gate.** Full suite after each lane is integrated, not once per
wave. Four lanes in one tree is exactly where per-lane passes hide cross-lane
breakage; the same check after the earlier six-lane merge found two real
defects. Costs roughly four minutes per lane and is worth it.

**No lane commits to the branch directly.** Each lane commits to its own branch;
the coordinator integrates one at a time with a full suite between. Six agents
committing to one branch produces a conflict nobody can bisect.

## Wave 1 completion criteria

- All four lanes merged, full suite green after each integration.
- Every control in the table above demonstrated to fail when its fix is reverted.
- `BarAggregator` and the `PositionSizer` factory exist as tested, injectable
  units with wiring notes — not yet registered.

## Programme completion criteria

1. A shipped strategy trades in paper from live ticks, and the same signal
   produces the same order size in backtest.
2. A crafted event payload cannot execute code on replay.
3. A restart cannot re-submit a live order or double-apply a fill.
4. Paper refuses an order the account cannot pay for, and never fills outside
   market hours.
5. Every credential file on disk is `0600`, asserted at boot.
6. Every durable write is fsync'd.
7. Every money-moving broker operation goes through the uncertain-submission
   tracker.
8. Recovery time is bounded by snapshot distance, not log length.
9. A fast test tier exists and runs in under a minute; two consecutive full runs
   in any order both pass.
10. No docstring describes behaviour that does not exist.

Items 1 and 2 are the ones that block deployment. The rest is scheduled work.
