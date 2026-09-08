# Phase B Brief — delete `trading/events/` after porting its two good pieces

**One-line goal:** remove the second OMS stack without losing its two genuinely good ideas (durable append-only event log, crash recovery replay), by porting them into `execution/` as the durability layer for the one pipeline.

**Why this is Phase B, not Phase A:** deletion is only safe after the quant-correctness hardening is green and the thing we are deleting does not overlap the layer we are hardening.

**Success:** no tracked file imports `trading.events`; the suite is green with the port in place; parity goldens are byte-stable; LOC delta is negative.

---

## What to port (only these two)

1. **Durable append-only event log** → `execution/event_log.py`. This becomes the engine lifecycle durability layer alongside the existing `SQLiteOrderStore`. It should serve the one pipeline’s lifecycle events, not a second OMS.
2. **Session recovery / replay-project-then-resume** → `execution/recovery.py`, driven off that log.

Everything else in `trading/events/` is the second OMS universe and is the deletion target.

---

## Order

1. Port event log + recovery into `execution/`.
2. Wire them as the engine lifecycle durability layer in boot where the old stack was referenced.
3. Run the full suite. If any behavior in `trading/events/` was the only home for something, the suite catches it before deletion.
4. Remove `trading/events/` and `trading/scripts/probe_review_fixes.py`. The state file already notes the physical `rm -rf` is blocked by tool policy, so the manual delete is the last physical step after the code port is verified.
5. Remove any tracked imports of the dead stack.

---

## Guardrails

- No new public API surface beyond what the durability layer needs.
- The port must serve the one pipeline; it must not resurrect a second order state machine.
- If porting reveals behavior that looks like a second OMS, stop and decide whether to delete that behavior instead of porting it.

---

*Skill posture: using-superpowers — treat deletion as a first-class refactoring with a red/green harness. Ponytail — port only the two pieces that earn their keep; delete the rest.*
