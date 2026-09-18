# Making the verification commands verify their artifacts

**Date:** 2026-09-13
**Scope:** every test, generator and smoke script in the repo whose result depends on
build output rather than on source.

## The class of defect

A verification command is only evidence if the artifact it reads is the artifact the
source produces. Three ways that stops being true, all of them silent:

- the command reads a **built directory** (`frontend/dist`, the library's `dist/`) that
  was not rebuilt after the source changed;
- the command reads a **checked-in fixture** that was captured from a different build
  than the one the repo pins;
- the command's **claim is stale** — it asserts something that was true of an earlier
  revision (a marker string, a header, a field) and now passes, or fails, for a reason
  unrelated to what it says it tests.

The audit found all three. Two were load-bearing: the Python parity gates were
asserting agreement with a library build nobody runs, and the browser suite could pass
against a bundle that no longer corresponded to the tree.

## What was verified, and by what

| command | claimed | now verifies | negative control that fires |
|---|---|---|---|
| `npm run build` (frontend) | builds the host | writes `dist/BUILD_STAMP.json` naming the sources and library build it came from | — |
| `npm run verify-artifact` | dist is current | sources hash + dist hash match the stamp | edit a source file → *built from different sources*; edit a dist file → *modified since it was built* |
| `npm run e2e` | the browser suite tests the app | `e2e/global-setup.ts` runs the stamp check before the first test | append a comment to `src/main.ts`, run bare `npx playwright test` → suite refuses to start |
| `scripts/regenerate-goldens.sh` | regenerates ground truth | refuses unless the checkout is at `openalgo-charts.pin` **and** its `dist/` is newer than its `src/` | bump the pin → *is at a0d4d66… but the goldens are a snapshot of deadbeef…*; `touch` a library source → *dist is older than src* |
| `pytest trading/tests/analytics` | backend matches the library | `test_goldens_provenance.py` checks the record against the pin, the fixtures hash and the goldens hash | edit one golden → *not the set the record describes*; change the recorded SHA → *captured from … but openalgo-charts.pin is …* |
| `trading/scripts/e2e_smoke.py` | the UI is mounted | every file in the stamp is fetched and byte-compared to its built hash | serve a dist without a stamp → check fails (locally) |
| `pytest trading/tests/interface/test_ui_mount.py` | the mount serves the built UI | the *real* dist, unpatched, is served byte-for-byte against its stamp | — |

## Where the build output was trusted

### 1. The golden generators read a different library than the pin

`scripts/generate_goldens*.mjs` imported the library by an absolute path:

```js
import { BUILTIN_INDICATORS } from
  "/Users/apple/Downloads/openalgo-charts-master/dist/openalgo-charts.indicators.mjs";
```

That checkout is at **2.1.7**; the repo pins **2.1.8**. So every fixture the parity
suite treats as ground truth was a snapshot of a version nobody was running, and
nothing could tell — the parity tests read the fixtures, never the library.

The generators now resolve the library through `scripts/library-dist.mjs`, which
refuses to hand it over unless the checkout is at the pin, `dist/` is newer than
`src/`, and the export the caller asked for resolves through the library's own
`exports` map. `OAC_ALLOW_UNPINNED=1` exists for a deliberate experiment against a
candidate version and says so on stdout — and in the record — rather than being the
quiet default.

### 2. The goldens had no provenance at all

Nothing said where 144 checked-in fixtures came from, so a partial regeneration or a
hand-edited file looked exactly like ground truth. `PROVENANCE.json` is now written
next to them by `scripts/write-provenance.mjs`, **once, at the end of the pipeline** —
not by each generator. That placement is the point: a generator that wrote the record
itself would describe a directory only it had finished updating and list only itself,
which is a claim broader than what it checked. A generator run on its own now leaves
the record stale, and the gate says so.

The hashes are over file *contents*, and `scripts/hash-tree.mjs` documents the
algorithm — relative path, NUL, bytes, NUL, in sorted path order — because
`test_goldens_provenance.py` re-implements it in Python and the two must agree.

### 3. The capture was truncated

The indicator generator captured only the series the descriptor *declares*
(`d.plots`). At 2.1.8, `consolidation-breakout` declares `rangeHigh`/`rangeLow` and
also returns `breakUp`, `breakDown` and `insideAge` (hidden series the host paints
through its tint hook). The golden lost them, and the parity gate — which iterates the
*backend's* keys and looks each one up in the golden — died on a missing key, which
reads like a maths regression and is really a capture bug.

Fourteen indicators had undeclared series. The generator now captures the union of
declared and returned series, logs the undeclared ones, and treats a series as an
array of numbers (arrays of objects — footprint cells, pivot records — belong to the
profile goldens).

### 4. What regenerating against the pin then revealed

The 2.1.7 → 2.1.8 diff in the fixtures is real, and it is not "widget plumbing":

- **input defaults changed** — `sma.length` 20 → 9, `wma.length` 20 → 9,
  `stochastic.kSmoothing` 3 → 1, and new colour inputs appeared on many descriptors.
  This is why the fixtures looked fine and the values were not: the parity gate
  derives its parameters from each golden's own recorded settings, so a golden
  captured under the old defaults is self-consistent and tests nothing about the new
  ones.
- **`supertrend` dropped the `bodyMid` plot.**
- **`footprint` grew a result shape** — `minDelta`, `maxDelta`, `rowSize`,
  `tradeCount`, and `open`/`close`/`high`/`low` when there are trades. The backend
  returned three fields, so the profile parity gate failed on the regenerated golden.
  `compute_footprint()` now implements all of it, accumulating the running delta
  extremes in *trade* order exactly as the reference does (the trailing sum is not
  the same value as a sum over sorted cells, and min/max over an accumulator is
  order-sensitive).
- **CLAUDE.md said the opposite.** "The 2.1.0 → 2.1.8 bump touched only widget/host
  plumbing, so the fixtures stayed valid" — the fixtures were not valid, and that
  sentence is why nobody looked. Corrected there, with what to actually do.

### 5. The smoke script's UI check asserted a marker that never existed

```python
check("ui.mounted", code == 200 and '<div id="setmodal"' in body.get("body", "") …)
```

`setmodal` appears nowhere in `frontend/` — it is upstream's sample markup, and this
host's `index.html` is its own. The check could not pass; it survived because it only
ran against a live server, which CI never starts. (The same file's order-spine checks
were failing too: `/orders` requires an `Idempotency-Key` header that the script did
not send, so two of its claims about placing orders had been failing for a while.)

The mount check now asserts the app shell, and the artifact claim is made properly:
every file in the stamp is fetched over HTTP and compared byte-for-byte against the
hash of the file that was built. `24/24 passed` here, against `15/22` before — the
seven difference being five UI mismatches and the two order checks.

### 6. The mount's own test never looked at the mount's real input

`test_ui_mount.py` said "opt-in by presence of frontend/dist" and always patched in a
synthetic `dist` — so nothing in the fast Python suite read the real artifact. The
synthetic tests stay (they test the wiring against any disk), and a second class
checks the real `frontend/dist`, when a stamped one exists, is served byte-for-byte —
including that `/ui/` is the untouched `index.html`, while `/` is the rewritten copy
that injects the API key. Those differ, and only a byte comparison can tell.

### 7. The pin had four copies

The workflow carried the SHA in `env:`, the docs carried it in a shell snippet, and
the generators carried a path to a checkout instead. Now `openalgo-charts.pin` is the
one file; the workflow reads it into `$GITHUB_ENV`, the build stamp records it, the
generators check it, and the pytest gate asserts the fixtures came from it. A second
copy is a second thing to forget to bump.

## Limits, stated rather than papered over

- **The stamp proves the bundle is the build of the current sources. It does not prove
  the build is correct** — only the browser suite does that, and only for what it
  covers.
- **The provenance hash proves the fixtures have not changed since the record was
  written.** It cannot prove the numbers in them are the library's; only regenerating
  from a pinned, freshly built checkout can, which is why the generators refuse to run
  against anything else.
- **`goldens/settings2/` has no generator in this repository.** It is hashed, so an
  edit is caught, but it cannot be reproduced from a checkout — worth knowing before
  treating it as a snapshot of anything.
- **The backend's catalogue defaults still disagree with the pinned library**: the
  catalogue offers `sma.length = 20` and `stochastic.kSmoothing = 3` where 2.1.8's
  descriptors default to `9` and `1`. The parity gates do not see this, because they
  take their parameters from each golden's recorded settings. Changing what a default
  indicator computes is a user-visible decision, so it is reported here rather than
  changed in passing.
- **The lib-freshness check is by mtime.** It catches the real case (a `git pull`
  inside the library) and can be fooled by a checkout that rewrites timestamps without
  changing bytes — which is why the durable proof is the content hash of `dist/`
  recorded in `PROVENANCE.json` and in the build stamp.
