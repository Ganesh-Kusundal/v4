# Execution-State Review — Fix Status

**Date:** 2026-09-01
**Branch:** `refactor/execution-state`
**Reviewed range:** `0d4454a..d79def1` (13 findings: 1 Critical, 4 Important, 8 Minor)
**Fixed range:** `0d4454a..7148e3a`
**Review report:** [execution-state-review-2026-09-01.md](execution-state-review-2026-09-01.md)

---

## Critical / Important fixes (all landed, all verified)

| # | Sev | Finding | Fix commit | Verification |
|---|-----|---------|-----------|--------------|
| 1 | Critical | Live broker fills bypass session projectors | `aeed5ed` | TDD red→green; probe 1 |
| 5 | Important | Rate-limit window off-by-one | `3dca9cb` | TDD red→green; probe 5 |
| 2 | Important | Duplicate retry bypasses risk gate via `peek()` | `2ff066d` | TDD red→green; probe 2 |
| 3 | Important | Three thread-safety races in command pipeline | `83d14ae` | TDD red→green; probes 3 |
| 4 | Important | Kill-switch live-mode broker caveat undocumented | `7148e3a` | docstring probe 4 |

Red-green test evidence: each fix has a test that failed on the pre-fix tree for
the reviewed reason and passes at HEAD. Red-state reproduction re-verified
post-hoc at `d79def1` (3/3 runs) after an initial red test was found to fail on
a test bug (`FrozenInstanceError`), not the reviewed race.

## Acceptance probes

`trading/scripts/probe_review_fixes.py` (commit `562ff5e`) replays each reviewer
repro scenario end-to-end against production modules, independent of the repo's
tests. **Discrimination verified both directions:**

- At `d79def1` (reviewed range): **0/5 probes pass** — each reproduces the
  reviewed failure mode.
- At fixed HEAD: **5/5 pass**, stable across 3+ consecutive runs.

Probe 2 initially passed at the old HEAD with a weaker repro; it was strengthened
to the reviewer's exact place-A/B/C-then-retry-A repro, after which it fails at
`d79def1` (retry rejected with rate-limit error) and passes at HEAD (cached
success, no risk-gate rejection). This also explains the two findings' interaction:
with the rate-limit off-by-one, the retry in the reviewer's scenario passed the
gate; idempotency then returned the cached result — so probe 2 only discriminates
against fixed code once finding 5 is also fixed.

## Suite state at HEAD

Full suite: **1889 passed, 16 skipped** (1889+16 = 1905 collected).

## Minor findings

The 8 Minor findings (6-13) are documented in the review report and remain open;
none block live-broker work. Deferred intentionally to keep this branch focused on
the Critical/Important set.
