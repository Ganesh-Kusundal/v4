# Lane 4a: Tree consolidation feasibility

**Verdict: the proposed consolidation is a REWRITE, not a mechanical move. The spec's
stated motivation does not hold. Recommendation: defer 4a/4b/4e, and do the parts of
Wave 4 that are actually load-bearing (4c event store, 4d dead-code, 4f docs) without
touching the package graph.**

Verified against the tree 2026-09-28. Every number below comes from a command that was
actually run, not from the spec.

## The central question

Does moving a package change the import paths that code uses, or only the directory layout?

**It changes the import paths.** The spec (line 129-133) merges seventeen workspace
members into five packages:

| Target | Members |
|---|---|
| `tradex_domain` | `domain` |
| `tradex_config` | `config` (stays a separate leaf) |
| `tradex_core` | `execution`, `application`, `reactive`, `observability`, `research`, `analytics` |
| `tradex_ingest` | `brokers`, `market_data` |
| `tradex_app` | `runtime`, `interfaces`, `strategy`, `replay` |

Merging `execution` into `tradex_core` moves
`execution/src/tradex_execution/engine.py` to `core/src/tradex_core/engine.py`, so
`from tradex_execution.engine import ExecutionEngine` must become
`from tradex_core.engine import ExecutionEngine`. This is a rewrite of every importer.

## The spec's stated motivation is false

The spec describes the tree as "flat with duplicate module names across packages."
Checked: for every leaf module across all members that fold together, the only duplicate
leaf names are `__init__.py` (expected) and **one** genuine collision:

```
execution/src/tradex_execution/engine.py   ExecutionEngine   (1,617 lines)
analytics/src/tradex_analytics/engine.py   AnalyticsEngine   (394 lines)
```

- `tradex_core` group (execution, application, reactive, observability, research,
  analytics): one collision, `engine.py`, in exactly two of the six members.
- `tradex_ingest` group (brokers, market_data): **zero** collisions.
- `tradex_app` group (runtime, interfaces, strategy, replay): **zero** collisions.
- Nested subpackage collisions (e.g. `paper/adapter.py` vs `upstox/adapter.py`): **zero**
  across all three groups. Those are within their own subnamespace and do not collide.

So the tree is not flat, and duplicate module names are not a problem that needs solving.
One collision exists, and it is a strong argument *against* the merge: merging creates the
collision that the current layout avoids.

## Blast radius

Import statements that must be rewritten, per merge target:

| Target | Import refs | Files containing them |
|---|---|---|
| `tradex_core` | 933 | 303 |
| `tradex_ingest` | 995 | 174 |
| `tradex_app` | 711 | 242 |
| **combined** | **2,639** | **719 unique files (94% of 767 tracked `.py` files)** |

(A second count including test files and all reference forms puts total `tradex_*`
import statements at 4,137. Either way the order of magnitude is the same: thousands.)

Highest-churn symbols, from actual greps:

- `from tradex_execution.engine import ExecutionEngine` — 61 occurrences
- `from tradex_execution.trading_cache import TradingCache` — 55
- `from tradex_execution.fill_sources import SimulatedFillSource` — 44
- `from tradex_analytics.indicators import (...)` — 18
- `from tradex_reactive.bus import ...` — within 145 statements across 129 files

## What else breaks besides imports

1. **The Wave 4 gate instrument would need rewriting itself.**
   `tests/test_import_boundaries.py` hardcodes `_SRC` (16 paths), `_INTERNAL` (17 names),
   `_ALLOWED` (17 package keys) and `_MODULE_TO_PKG`. Every entry changes. The tool that
   is supposed to *verify* the consolidation is itself written against the old graph.

2. **Distribution names would collide.** 17 members each declare a dist name today, 1:1
   with their module root. Six members all declaring `name = "tradex-core"` is not
   possible in one workspace. The merge therefore requires either keeping 16 dist names
   alongside 5 module roots (a deliberate, documented split), or consolidating dist names
   and re-declaring every dependency. Both are extra work and extra risk beyond the
   import rewrite.

3. **The editable install is the load-bearing resolution mechanism, and it is exactly
   the trap to watch for.** `pyproject.toml` line 31 says "Editable installs own extracted
   packages; pythonpath keeps composition + shims." Verified: the venv carries one
   `_editable_impl_tradex_<pkg>.pth` per package, and each one holds an ABSOLUTE path into
   this main checkout:

   ```
   .venv/lib/python3.13/site-packages/_editable_impl_tradex_execution.pth
     -> /Users/apple/.../v4/execution/src
   ```

   This is why a `git worktree` does NOT isolate source for testing: the venv resolves
   `tradex_execution` to the main checkout regardless of which worktree you run pytest
   from. A merge that moves `execution/src` therefore invalidates every one of these
   `.pth` files, and the venv must be reinstalled, not just the `pythonpath` list edited.
   The 18-entry `pythonpath` list in root `pyproject.toml` and the 1 `sys.path.insert` in
   `conftest.py` are secondary.

   This is the same trap that produced the audit's false "18 pre-existing failures on a
   pristine commit" claim: the reproduction ran in a worktree but imported modified main
   source through these `.pth` files.

4. **`[tool.uv.sources]`, `[tool.uv.workspace]` members, per-member
   `[tool.hatch.build.targets.wheel]` `packages` keys, and root `testpaths`** all name the
   old layout and all need editing.

## Per-move verdicts

| Move | Verdict | Notes |
|---|---|---|
| `domain` | **MECHANICAL** | Spec says "Unchanged." Verified correct: zero internal deps, zero importers to rewrite. |
| `config` | **MECHANICAL** | Spec says stays a separate leaf. Verified correct. |
| `ingest` (brokers + market_data) | **REWRITE** (174 files) | Zero collisions. Cleanest of the three merges if it were to be done at all. |
| `app` (runtime + interfaces + strategy + replay) | **REWRITE** (242 files) | Zero collisions. |
| `core` (execution + application + reactive + observability + research + analytics) | **REWRITE** (303 files) | Plus the `engine.py` collision must be resolved by renaming one of them, which adds churn on top of the 303. |

## Recommendation

**Defer.** Reasoning, bluntly:

1. The stated benefit (duplicate module names) does not exist except in one place, and the
   merge would *create* that collision rather than remove it.
2. The cost is 2,639 import statements across 719 files, plus rewriting the very test
   that is supposed to gate the change, plus dist-name consolidation.
3. **Wave 4's own spec (line 414-417) says this wave "does not run concurrently with
   the rest of the programme"** because "moving tests and deleting modules collides with
   the code other waves are editing." That constraint is real and it is the reason Phase 2
   must wait for Phase 1 regardless.
4. Nothing about the current 17-member layout is broken. The boundary test passes on it.
   The package extraction this wave used to own is already done (`b941da2`).

The valuable parts of Wave 4 are 4c (event store hardening), 4d (dead-code removal), and
4f (documentation accuracy). None of them require moving a single package, and 4c/4d/4f
are already in flight in Phase 1.

## Commands that established this

```bash
# leaf-module collisions per merge group
for grp in "core:application reactive observability research analytics execution" \
           "ingest:brokers market_data" \
           "app:runtime interfaces strategy replay"; do
  name="${grp%%:*}"; members="${grp#*:}"
  for m in $members; do
    (cd "$m/src" && find . -name '*.py' -not -path '*/tests/*' | sed 's|.*/||')
  done | sort | uniq -d
done

# import-reference blast radius
for pkg in application reactive observability research analytics execution \
           brokers market_data runtime interfaces strategy replay; do
  grep -rn "import tradex_${pkg}\|from tradex_${pkg}" --include=*.py . | grep -v '/\.venv/'
done

# the single real collision
find application execution -name 'engine.py' -path '*/src/*'
grep -n '^class ' analytics/src/tradex_analytics/engine.py execution/src/tradex_execution/engine.py
```
